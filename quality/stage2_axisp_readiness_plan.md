# Stage-2 (Axis-P coverage) — readiness plan (NOT started; gated on owner's exit-line ①)

Status: **prepared, not implemented.** Stage-2 begins only on the owner's exit-line
approval (① pass N-gate → Stage-2). This is a readiness artifact so execution is
immediate on approval. Source of truth: GPT's owner-free ruling
(`/tmp/gpt_ownerfree_answer.md`) + the /goal Stage-2 spec, with the GPT-ban adjustment
below.

## Core design (owner-free; GPT-decided pre-ban)
- **Decider**: RED only from **explicit / confirmed / closed-world contracts** via
  deterministic rules. Strong-model judgement = **amber recall only**, never red.
  Owner is **never a per-item step**.
- **Owner-free flow**: a model/structural gap → **amber Finding/AskItem
  (blocking=false, RECOMMENDED_PROCEEDING)** → persisted in `coverage_decisions` →
  CI/merge/loop never wait → anyone (owner/reviewer/PM) batch-confirms later →
  CONFIRMED = contract → only then red-enforceable. Default-profile recommendations are
  NOT contracts.
- **Axis-P gate**: explicit/confirmed/closed-world contracts deterministically checked +
  their violations red + model-discovered gaps surfaced as amber (not hidden) + no owner
  answer required for CI/merge + amber residue persisted + corpus PCUMR. Must NOT: ask
  owner per gap / convert model confidence to red / treat default recs as contracts /
  show pure green while amber semantic residue exists.

## GPT-ban adjustment (2026-06-25)
GPT is banned. Everywhere the design said "strong-model" for **amber recall** / the
**Advisory Union**, use **Claude (Sonnet + Opus)** — still independent multi-engine
(round 16 proved they catch each other's misses). The **deterministic RED decider is
model-independent (rules, not a model)**, so it is unaffected — model-independence holds.

## Implementation steps (on ① approval)
1. **coverage_decisions / AskItem first-class**: schema ASK / RECOMMENDED_PROCEEDING /
   CONFIRMED / OVERRIDDEN, `blocking=false`; CI/merge/loop never block on pending. Reuse
   existing `ProjectLexicon.coverage_decisions` + `HitlSession` + `ask_user_question_adapter`.
2. **owner answer → contract conversion**: CONFIRMED answers become explicit contracts in
   user_journeys / resource_flow / negative_space / e2e-signal (the deterministic-check
   inputs). Only then are violations red.
3. **`codd check` positive materiality**: display covered / implicit / gap / pending /
   contracts / traceability (positive coverage, not just negative findings).
4. **PCUMR + E-PCUMR**: corpus PCUMR vs frozen + construction-derived gold;
   real-project E-PCUMR (explicit-contract coverage). Advisory Union = Claude 2-engine
   amber backlog.
5. **5-fixture positive corpus** (owner-seeded gold + construction-derived): missing
   journey / missing producer / negative-space / NFR / acceptance-signal.

## Invariants (unchanged)
anti-false-green / anti-false-red (RED only owner-confirmed-or-explicit-contract) /
generality / Contract Kernel / model-independence / owner-not-a-bottleneck. Axis-N
(systematic regression + niche adversarial-symlink) maintained as ongoing Claude
2-engine review (20-30%).

## Pre-flight reminders
- OpenAI (GPT-CDP / Codex) stays OFF (account ban) — Claude only.
- Red-before-green + 5-fixture mutation corpus + full-suite gate per change (unchanged).
- Opus does not self-certify Stage-2 completion either — owner is the strategic gate for
  any new-meaning decision.
