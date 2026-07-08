# False-green vectors — decision memos (owner-gated / consult-needed)

Per `/goal`, vectors that need NEW meaning (new concept, schema/DSL, severity
change, new red for existing users, absence guarantees) or a new mechanism are
**not** auto-implemented. They are parked here with the design question so the
owner + GPT Pro can resolve them efficiently. Autonomous boundary: *a red
logically derivable from existing contract semantics is autonomous; anything
that needs a new decision to judge red/green is owner-gated.*

Status legend: `consult` = needs GPT-Pro design before code; `owner` = needs an
owner decision on meaning/severity; `fixtures-ready` = behaviour exists, only
the 4-fixture lock is pending (safe to do autonomously).

Shipped already (for reference): `resource_supply_use_dangling` (v3.3.0, RED),
`produced_never_consumed` / dead_resource (v3.4.0, AMBER).

---

## resource_order_explicit_flow — `consult`
**Vector:** a producer exists but is ordered *after* its required consumer.
**Why parked:** `resource_flow_coherence` v1 is existence-based. To judge order
we need a producer→step and consumer→step mapping. Capability contracts do not
reference operation-flow steps today → adding that linkage is a new mechanism.
**Design question (for GPT):** reuse existing `operation_flow` ordering only, or
add an optional `step:` reference on consumes/produces (new schema → owner-gated)?
**Safe rule:** red ONLY when explicit ordering exists and places producer after
consumer; no ordering info → skip (never infer order → false-red).
**false_red_risk:** 3.

## extractor_silent_noop — `consult`
**Vector:** a broken capability regex / malformed `resource_contract` /
unsupported file extension makes a declared contract silently vanish (warn→drop)
and the run stays green.
**Why parked:** the extractor already *warns* (`_warn_or_raise`) but those
warnings never reach `verify`. Surfacing them needs a new channel
(extractor → DAG node attribute or a run-level diagnostics list → a check).
**Design question (for GPT):** where to carry extraction diagnostics without
polluting the language-free core; amber advisory vs (owner-gated) red.
**Safe rule:** "if a declared contract cannot be extracted, that is *visible*,
never silently green." Visibility = autonomous; making it red = owner-gated.
**false_red_risk:** 2.

## brownfield_stage_skip — `owner`
Surfacing shipped (stage_status + PARTIAL headline, exit code unchanged).
`brownfield_stage_skip` exit-code escalation — OWNER-GATED (a nonzero exit on a
skipped/partial stage is a NEW red for existing users).

Interim contract until the owner decides (Fable5 review#2 N1): **CI consumers must
gate on `stage_status` in `.codd/brownfield_report.json` (or the md `PARTIAL`
headline), NOT on the exit code** — a skipped/partial/reused run still exits 0.
Recommended owner decision: add an opt-in `--fail-on-partial` flag that mints the
nonzero exit (a new exit-code semantic is itself an owner-gated F4 call).

## identity_alias_drift — `consult`
**Vector:** the same resource/capability is written under different spellings and
treated as distinct (or wrongly unified).
**Why parked:** `resource_flow_coherence` resolves *declared* aliases already.
Catching *undeclared* drift needs similarity/normalisation heuristics = new
mechanism (and high false-red risk if fuzzy).
**Design question (for GPT):** red only on "an explicitly declared alias whose
canonical target is never declared" (derivable, low risk) vs fuzzy near-miss
detection (owner-gated). Start with the former.
**false_red_risk:** 3.

## assertion_abuse — `owner`
**Vector:** a journey/test exists but asserts presence/render, not the expected
outcome. The single most false-green-prone class.
**Why parked:** "sufficient assertion" is a semantic judgement → defining it is
new meaning (owner-gated). `user_journey_coherence` already heuristically flags
no-assertion steps as amber.
**Design question (for owner):** what counts as a real outcome assertion? Start
by *corpus-ing* assertion-abuse fixtures (observe missed_green) before any red.
**false_red_risk:** 5.

## cross_artifact_partial_coverage — `owner`
**Vector:** only a subset of declared design/impl/test is present, yet green.
**Why parked:** `implementation_coverage` has a historical glob `path_hint`
quirk that suppresses additional-implementation pass; changing severity touches
existing users → owner-gated.
**Design question (for owner):** lock the current behaviour with a fixture first;
any severity change is owner-gated.
**false_red_risk:** 4.

## cardinality_partial — `owner`
**Vector:** a 1:N obligation is satisfied/asserted for one member only.
**Why parked:** "all / at-least-one / representative" is owner-defined meaning.
`_one_to_many_detection` gives a schema-light scaffold to build on.
**false_red_risk:** 4.

## semantic_conflict — `owner`
**Vector:** two obligations impose contradictory constraints on one resource.
**Why parked:** requires a conflict-semantics definition (new concept).
**false_red_risk:** 5.

## negative_space — `owner`
**Vector:** required ABSENCE (no PII in logs, deleted data not re-exposed, no
access without permission) is uncheckable.
**Why parked:** absence needs an observation model = new concept. Start with a
forbidden-evidence fixture + owner decision memo only.
**false_red_risk:** 5.

## stale_evidence — `owner`
**Vector:** old test/extraction results pass as current green.
**Why parked:** freshness model (mtime/hash) is environment-sensitive → easy
false-red across machines. Start from dogfood fixture + diagnostic display.
**false_red_risk:** 4.

---

## Safe-to-do autonomously (queued, not blocked)
- `check_selection_drift` — behaviour already exists (`unselected_check_names`
  surfaces a notice in `cli.py`). Only the 4-fixture lock is pending
  (`fixtures-ready`).
- `diagnostic_incompleteness` (P2) — ensure each red carries a remediation hint;
  low-risk diagnostic improvement.
