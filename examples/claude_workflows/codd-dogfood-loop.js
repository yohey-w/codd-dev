// ═══════════════════════════════════════════════════════════════════════════
// codd-dogfood-loop.js — Claude Code Agent Workflow for CoDD's self-verification
// loop ("run the quality gates against CoDD itself until problems stop emerging").
//
// WHAT THIS IS
//   A supervisor template for Claude Code's Agent Workflows that drives ONE
//   iteration of the dogfood verification loop documented in dogfood/README.md.
//   The loop runs CoDD's own quality gates against CoDD and its fixtures across
//   14 verification axes (D1..D14). Run this workflow repeatedly until the
//   convergence report prints `converged: true`.
//
//   The loop has two halves:
//     1. SCRIPTED axes (D7, D8, D10, D11, D14) — free, deterministic, no LLM.
//        One Bash call to `python dogfood/run_iteration.py` runs them all,
//        folds any findings into dogfood/ledger.yaml, and prints the
//        convergence report. This half is fully automated.
//     2. LLM axes (D1-D6, D9) — input-robustness and lifecycle checks that need
//        a real model run. The system-under-test runs on its ASSIGNED executor
//        (README "Executor assignment" + the ledger's model_roles / sut_model):
//        the cheapest viable model with Sonnet 4.6 as the floor for D1-D5/D9, and
//        Codex (gpt-5.5 xhigh) for the cross-CLI axis D6. A lightweight model is
//        the most sensitive instrument for surfacing ambiguity (M1). Each run is
//        recorded by hand into the same ledger.
//
// EXECUTOR ASSIGNMENT (see dogfood/README.md "Executor assignment")
//   (2026-07-01: Fable 5 — export-restricted since 2026-06-12 — and GPT-5.5 Pro
//   consult — unusable since 2026-06-30 — are BOTH gone. There is no longer a
//   second, architecturally-different model to consult as an external divergence
//   layer. Opus 4.8 effort=max extended thinking + explicit self-critique is now
//   the sole divergence mechanism; see dogfood/README.md and
//   memory/feedback_gpt55_strategic_layer.md for the full rationale.)
//   - Loop machinery + the scripted axes: NO model (deterministic, free, Tier 0).
//   - SUT executor: cheapest viable model, Sonnet floor (Tier 1); Codex
//     (gpt-5.5 xhigh) for D6 — this is the Codex CLI as a system-under-test, a
//     distinct and still-valid mechanism from the retired GPT-Pro-consult layer.
//   - This driver itself, plus MECHANICAL triage/fix (a bug patch to existing
//     behaviour), run on Opus 4.8 (Tier 2 — strong, episodic).
//   - CONCEPTUAL findings (the fix is a NEW abstraction / NEW check-class, not a
//     patch — precedent: the D13 visual-judge finding that birthed the
//     environment-coverage axis) and the periodic PORTFOLIO-EVOLUTION pass ("which
//     failure class does NO current axis cover?") are Tier 3 — apex: an Opus 4.8
//     effort=max extended-thinking design pass with EXPLICIT self-critique
//     (necessary? sufficient — does it cover sibling cases? general — does it fix
//     the class, not just this symptom? per feedback_fix_strategy_output_then_critique),
//     then committed+pushed AUTONOMOUSLY like any other fix (NO owner-approval
//     gate) — UNLESS the self-critique pass concludes the finding is genuinely
//     novel PRODUCT direction with no training-data precedent, in which case it
//     is marked wontfix and escalated to the owner (see the TRIAGE phase below).
//     The mechanical/conceptual split now routes how much the design agent
//     self-critiques before acting (mechanical = direct patch; conceptual =
//     self-critique pass first), not which model is consulted — both routes are
//     Opus 4.8. The whole stack = "the same apex model designs with explicit
//     self-critique in place of external divergence, the execution model
//     executes" applied recursively to the QA loop.
//
//   The workflow VEHICLE is Claude-Code-specific, but every `codd` command it
//   triggers uses whatever `ai_command` the project's codd.yaml declares — Claude
//   CLI, Codex CLI, or any text-in/text-out CLI. This template never assumes one.
//
// SELF-CONTAINED EQUIVALENT
//   `python dogfood/run_iteration.py` runs the scripted half as one detachable
//   command. This workflow adds the LLM-axis protocols and the triage step on
//   top, with visible per-axis progress and conversational failure triage.
//
// THE LOOP CONTRACT (see dogfood/README.md for the full protocol)
//   - SELECT the next case: a world-change trigger first (new model -> D5, new
//     stack -> D3, a user-reported repo -> D11, a new benchmark -> D12), else a
//     pending case from the ledger, else the least-recently-run unsaturated axis.
//   - RUN it on the cheapest viable executor.
//   - For EVERY finding: (a) file a fix behind the codd-improve Generality Gate
//     with a regression test so it can never silently return, and (b) DERIVE
//     follow-up cases into the ledger's pending_cases. A finding resets its
//     axis's saturation counter to 0.
//   - A finding-free run advances the axis toward saturation (K=2).
//   - The loop converges only when every axis is saturated AND no pending case
//     is open — a moving target by design, because findings keep minting cases.
//
// HONEST LIMITS (the owner's domain — dogfood is structurally blind to these)
//   New-concept divergence, taste, and the self-hosting limit are human-judgment
//   calls, not loop signals. When a finding turns out to be one of these, mark it
//   `wontfix` in the ledger with a note instead of "fixing" it.
//
// INSTALL
//   1. Copy this file into your CoDD checkout: .claude/workflows/codd-dogfood-loop.js
//   2. Ensure `python` and `codd` are on PATH and dogfood/ exists.
//
// RUN
//   In Claude Code:  /workflows codd-dogfood-loop
//   Optional arg: a single axis id to focus this iteration on (e.g. D6), or
//   "scripted" to run only the free deterministic half.
//     /workflows codd-dogfood-loop D6
//     /workflows codd-dogfood-loop scripted
// ═══════════════════════════════════════════════════════════════════════════

export const meta = {
  name: "codd-dogfood-loop",
  description:
    "CoDD self-verification loop: run the quality gates against CoDD across 14 " +
    "axes, fold findings into dogfood/ledger.yaml, and report convergence. " +
    "Run repeatedly until `converged: true`.",
  phases: ["scripted-axes", "select-llm-axis", "run-llm-axis", "triage", "record"],
};

// The AUTONOMOUSLY-RUNNABLE LLM axes and the README protocol each one follows.
// The SUT runs on the assigned executor (README "Executor assignment"): the
// cheapest viable model with Sonnet 4.6 as the floor for D1/D3/D4/D5/D9, and
// Codex (gpt-5.5 xhigh) for the cross-CLI axis D6. The driver/triage themselves
// run on Opus 4.8 (Tier 2).
//
// D2 is DELIBERATELY ABSENT here: it is OWNER-ONLY (see OWNER_ONLY_AXES below and
// dogfood/README.md "Owner-only axes"). An autonomous agent must NOT fabricate the
// "raw human requirements" D2 needs — doing so makes it both author and solver and
// collapses the gap D2 tests. So this driver never auto-selects D2; it fires only
// when the OWNER supplies a genuine requirements doc.
const LLM_AXES = {
  D1: "First-contact protocol — hand a non-builder's plain-language spec (loose/plural type names, prose) to `codd greenfield`. Do NOT pre-clean the input; the mess is the test.",
  D3: "Stack-rotation protocol — pick a language+framework+IaC not seen recently; `codd greenfield --language <lang>`; record stack-specific assumptions that leak.",
  D4: "Complexity-ladder protocol — re-run greenfield one rung up the size ladder; findings are usually resource ceilings (token / time / memory).",
  D5: "Lightweight-model protocol (modifier on D1-D4) — set ai_command to the weakest model that can plausibly finish (Sonnet 4.6 is the floor; Haiku only where it can complete), then run an input-family axis. Record findings under the input axis.",
  D6: "Cross-CLI protocol — set ai_command to Codex (gpt-5.5 xhigh) and re-run a known greenfield input end to end; this is the cross-CLI axis by definition, surfacing prompt/format/tooling coupling.",
  D9: "Lifecycle protocol — apply a sequence of codd-evolve / codd fix changes to a living generated app; after each, assert coherence (codd validate / codd doctor / codd diff).",
};

// OWNER-ONLY axes — tracked in the ledger but NOT autonomously runnable, and
// EXCLUDED from the autonomous convergence requirement (see dogfood/README.md
// "Owner-only axes (the author==solver collapse)"). The driver must never
// auto-select these or treat them as gating convergence; they fire only when the
// owner supplies a genuine input the agent must not fabricate.
const OWNER_ONLY_AXES = {
  D2: "Messy-requirements protocol — OWNER-SUPPLIED requirements doc only. An autonomous agent must NOT fabricate the doc (author==solver collapse). Skipped by autonomous selection; excluded from convergence.",
};

// Shared rails for every axis agent.
const COMMON = `
You are running ONE axis of CoDD's dogfood verification loop (see dogfood/README.md).
Rules:
- Read dogfood/ledger.yaml FIRST. Never restart the loop from memory — the ledger is the SSOT.
- Frame everything as developer-tool QA: verification rules, quality gates, input robustness.
- Run only the commands you are told to run, via Bash, from the repo root.
- Do not ask the user questions; this is an unattended loop iteration.
- A run that surfaces zero findings is a "dry run" and advances the axis toward saturation.
- End your reply with exactly one line: STATUS: OK or STATUS: FAIL — <one-line reason>.
`;

function failed(result) {
  return !/STATUS:\s*OK/.test(String(result ?? ""));
}

export default async function run({ args, agent, phase, log }) {
  const focus = String(args ?? "").trim();

  // ── Phase 1: the scripted, deterministic half (free, no LLM) ───────────────
  await phase("scripted-axes");
  log("[dogfood] running the scripted axes (D7, D8, D10, D11, D14)…");
  const scripted = await agent(`${COMMON}
Run the scripted half of the loop with one command:
  python dogfood/run_iteration.py
This runs every deterministic axis, folds any new findings into dogfood/ledger.yaml,
and prints the convergence report. Report STATUS: FAIL only if the command itself
crashes (a NEW finding is expected loop output, not a workflow failure — it makes
the command exit nonzero on purpose). Quote the convergence report in your summary.`);

  if (failed(scripted)) {
    log("[dogfood] scripted axes runner crashed — fix the harness before continuing.");
    return;
  }

  if (focus === "scripted") {
    log("[dogfood] scripted-only iteration complete. Re-run without 'scripted' to drive the LLM axes.");
    return;
  }

  // ── Phase 2: SELECT the LLM axis (or pending case) for this iteration ───────
  await phase("select-llm-axis");
  const explicitAxis = LLM_AXES[focus] ? focus : null;
  const selection = await agent(`${COMMON}
SELECT the next LLM axis to run this iteration. NEVER select an OWNER-ONLY axis or
an owner-only pending case (${Object.keys(OWNER_ONLY_AXES).join(", ")}) — these are
not autonomously runnable (fabricating their input makes you author AND solver,
collapsing the gap they test) and are excluded from autonomous convergence; in the
ledger their status is "owner-only", never "pending"/"running". Apply the README
SELECT rule in order, considering only AUTONOMOUS axes:
  1. A world-change trigger wins first: a new model released -> D5; a new
     framework/language/IaC in scope -> D3; a user reports an issue on a real
     repo -> D11 (already scripted; add the repo to dogfood/zoo.yaml instead);
     a new external benchmark -> D12.
  2. Otherwise prefer an OPEN entry in dogfood/ledger.yaml pending_cases whose
     axis is an autonomous LLM axis (${Object.keys(LLM_AXES).join(", ")}) and whose
     status is pending/running, highest priority first. SKIP any case whose status
     is "owner-only".
  3. Otherwise pick the least-recently-run unsaturated AUTONOMOUS LLM axis.
${explicitAxis ? `The operator pinned this iteration to ${explicitAxis}; select it unless a world-change trigger overrides.` : ""}
If the only remaining work is owner-only (e.g. D2 awaiting an owner-supplied doc),
report that there is no autonomous axis to run. Report the chosen axis id and the
one-line reason. End with: STATUS: OK — chose <axis>.`);

  if (failed(selection)) {
    log("[dogfood] no LLM axis selected (all saturated, or selection failed). Check the convergence report above.");
    return;
  }

  // Extract the chosen axis, but NEVER honour an owner-only axis (e.g. D2): those
  // are not autonomously runnable and must not be selected even if the model's
  // reasoning text mentions them. Fall back to the first autonomous LLM axis.
  const mentioned = (String(selection).match(/\bD(?:1[0-4]|[1-9])\b/g) || []);
  const chosen =
    mentioned.find((a) => LLM_AXES[a]) ||
    (explicitAxis && LLM_AXES[explicitAxis] ? explicitAxis : null) ||
    Object.keys(LLM_AXES)[0];
  if (mentioned.some((a) => OWNER_ONLY_AXES[a]) && !mentioned.some((a) => LLM_AXES[a])) {
    log(`[dogfood] selection resolved to an owner-only axis (${mentioned.join(", ")}) — not autonomously runnable. Skipping; supply a genuine owner input to run it.`);
    return;
  }
  const protocol = LLM_AXES[chosen] || LLM_AXES[Object.keys(LLM_AXES)[0]];

  // ── Phase 3: RUN the selected axis on the assigned SUT executor ─────────────
  // SUT executor matrix (see dogfood/README.md "Executor assignment" and the
  // ledger's model_roles / per-axis sut_model): cheapest viable model, with
  // Sonnet 4.6 as the FLOOR for the LLM input-family axes (D1-D5, D9), and
  // Codex (gpt-5.5 xhigh) for the cross-CLI axis (D6) by definition.
  const sutModel =
    chosen === "D6"
      ? "Codex (gpt-5.5 xhigh) — the cross-CLI axis by definition"
      : "the cheapest viable model, with Sonnet 4.6 as the floor (Haiku only where it can complete)";
  await phase("run-llm-axis");
  log(`[dogfood] running ${chosen} — SUT executor: ${sutModel}…`);
  const axisRun = await agent(`${COMMON}
Run axis ${chosen}.
PROTOCOL: ${protocol}
SUT-executor rule (M1): set the project's ai_command for the system-under-test to
${sutModel}. ${chosen === "D6"
      ? "D6 is cross-CLI, so the SUT must run on Codex (gpt-5.5 xhigh)."
      : "Use the cheapest viable model — Sonnet is the floor; do NOT use a stronger model than needed (a lighter model is the more sensitive instrument and surfaces gaps a stronger one papers over)."} Capture every defect you
observe as a candidate finding (symptom, the stage that produced it, and how to
reproduce). A clean run with no defects is a valid dry run.
Report the candidate findings (or "none"). End with STATUS: OK or STATUS: FAIL.`);

  // A failed axis RUN is itself signal: it usually means the harness/protocol is
  // broken, which is a finding. Continue to triage rather than aborting.
  const axisFindings = !/no findings|none\b/i.test(String(axisRun));

  // ── Phase 4: TRIAGE every finding (classify, design+fix, derive) ────────────
  // Triage CLASSIFIES each finding, which now routes how much self-critique the
  // SAME model (Opus 4.8) applies before acting — not whether a human approves
  // it, and not which model is consulted (Fable 5 / GPT-5.5 Pro consult are both
  // unavailable as of 2026-06-30; there is no second model to route to):
  // MECHANICAL (a bug patch to existing behaviour) → Opus 4.8 fixes directly
  // (Tier 2); CONCEPTUAL (the fix is a NEW abstraction / NEW check-class, not a
  // patch — precedent: the D13 visual-judge finding that birthed the
  // environment-coverage axis) → Opus 4.8 effort=max extended thinking with an
  // EXPLICIT self-critique pass first (Tier 3). BOTH branches then fix +
  // regression-test + commit + push AUTONOMOUSLY — there is no owner-approval
  // gate on loop findings UNLESS the self-critique pass concludes the finding is
  // genuinely novel PRODUCT direction with no training-data precedent, which is
  // marked wontfix and escalated to the owner instead (see below).
  await phase("triage");
  if (axisFindings) {
    log(`[dogfood] ${chosen} surfaced candidate findings — classifying, then mechanical→Opus 4.8 direct fix (Tier 2) / conceptual→Opus 4.8 self-critique design pass (Tier 3); both fix+commit+push autonomously (no owner gate, unless self-critique flags genuine novelty).`);
    await agent(`${COMMON}
TRIAGE the findings from axis ${chosen}. CLASSIFY each finding first, then act:

A) MECHANICAL finding (a bug patch to EXISTING CoDD behaviour, e.g. the type
   matcher ignored a plural, verify exited 0 on a red suite). Fix on the
   STRONGEST practical model — Opus 4.8 (M1: dogfood with the weak instrument,
   fix with the strong one; Tier 2, episodic). For EVERY mechanical finding do
   BOTH:
     1. File a FIX behind the codd-improve Generality Gate. The fix must
        generalize to CoDD's behaviour, never overfit to the dogfood subject
        ("the sample app needs X" is not a CoDD fix; "the implement stage
        truncates any large output" is). Ship a REGRESSION TEST so the finding
        can never silently return.
     2. DERIVE follow-up cases (a bigger app, the same input on another CLI, a
        new stack, the same weakness in a sibling vocabulary) and append each to
        dogfood/ledger.yaml pending_cases with origin "derived:<finding-id>".
   Mark the finding class: mechanical in the ledger.

B) CONCEPTUAL finding (the right fix is a NEW abstraction or NEW check-class, not
   a patch — precedent: the D13 human/visual-judge finding whose fix was the new
   environment-coverage axis). Do NOT patch this directly. Fable 5 and GPT-5.5 Pro
   consult are NOT available (export-restricted / unusable as of 2026-06-30) — there
   is no second, architecturally-different model to consult, so YOU (Opus 4.8,
   effort=max, extended thinking) are the design agent. Before implementing, run
   an EXPLICIT self-critique pass and answer in your reply: (a) Necessary — does
   the finding actually require a new abstraction, or does an existing check
   already cover it? (b) Sufficient — does the design cover sibling cases, not
   just the one observed symptom? (c) General — does it fix the class (a
   language-agnostic / stack-agnostic root cause), never a per-OSS or per-case
   patch? If the self-critique concludes this is genuinely novel PRODUCT
   direction with NO training-data precedent (not just "a new check-class for
   the loop," which is still loop-internal and fine to design autonomously),
   STOP and mark it wontfix per the escalation note below instead of proceeding.
   Otherwise, for EVERY conceptual finding do BOTH (exactly like the mechanical
   branch, AUTONOMOUSLY — there is NO owner-approval gate):
     1. Implement the designed abstraction / new check-class behind the
        codd-improve Generality Gate, with a REGRESSION TEST, then COMMIT + PUSH
        it like any other fix.
     2. DERIVE follow-up cases into dogfood/ledger.yaml pending_cases with origin
        "derived:<finding-id>".
   Mark the finding class: conceptual; record it (id, axis, symptom, fix_commit,
   regression_test) like any other finding.

The class now only routes how much self-critique the design agent applies before
acting (mechanical = direct patch; conceptual = self-critique pass first) — both
routes are Opus 4.8, and both branches normally end in an autonomous fix +
regression test + commit + push.

If a finding is actually new-concept divergence, taste, or the self-hosting limit
(the README's honest limits — genuinely novel PRODUCT direction with no
training-data precedent, not a loop-internal new-check-class), mark it wontfix
with a note instead — that is the owner's judgment, not a loop fix.
End with STATUS: OK once each finding is classified, fixed, and committed+pushed,
with regression tests and derived cases in place.`);
  } else {
    log(`[dogfood] ${chosen} was a dry run (no findings) — it advances toward saturation.`);
  }

  // PORTFOLIO EVOLUTION (anti-convergence, Tier 3 — apex, periodic): separate
  // from per-finding triage. Convergent case-derivation (this Opus driver) drifts
  // toward obvious variants of known axes; periodically run a DIVERGENT pass
  // asking "which failure class does NO current axis (D1..D14) cover?" to mint NEW
  // axes / check-categories. That "what are we NOT testing" judgment is the same
  // apex role as above (Opus 4.8 effort=max + explicit self-critique — Fable 5 is
  // no longer available to provide an external divergent head, so the
  // self-critique pass IS the divergence safeguard; autonomous, owner informed,
  // not gating), not a convergent job, and is best run occasionally (e.g. once
  // the portfolio has gone quiet) rather than every iteration — hence it is
  // documented here, not invoked on every tick.

  // ── Phase 5: RECORD the run into the ledger ────────────────────────────────
  await phase("record");
  await agent(`${COMMON}
RECORD this iteration in dogfood/ledger.yaml (the LLM axes are recorded by hand):
  - Append any new finding to the top-level findings list (id, axis ${chosen}, date,
    model_used, symptom, root_cause, fix_commit, regression_test, status, derived_case_ids).
  - Append derived cases to pending_cases; mark any consumed case done.
  - Update axis ${chosen}: a NEW finding resets saturation_counter to 0 and status
    to active; a dry run increments saturation_counter and sets status saturated at
    K=2. Set last_run to today.
  - If a fix touched a shared stage (extract / implement / verify / propagate),
    reset the saturation_counter of every sibling axis that exercises it (per the
    README RESET rule).
Then restate the convergence line: converged == every axis saturated AND no open
pending_cases. End with STATUS: OK.`);

  log(`[dogfood] iteration complete for ${chosen}.`);
  log("[dogfood] Re-run /workflows codd-dogfood-loop until the convergence report says: converged: true");
}
