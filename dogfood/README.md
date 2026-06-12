# The Dogfood Verification Loop

A **designed workflow** that runs CoDD against itself until *problems stop
emerging*. The loop is self-perpetuating: every finding spawns both a **fix**
and one or more **derived cases**, so the work queue keeps refilling itself
until every axis goes quiet. State lives in [`ledger.yaml`](./ledger.yaml) and
survives sessions — **restart the loop by reading the ledger, never from
memory.**

This is the **SSOT for any future agent resuming the loop**. It is not a CI
script you run once; it is a protocol a human or an agent follows one iteration
at a time, recording each step in the ledger.

The portfolio of 14 axes (D1..D14) and the three meta-principles (M1/M2/M3) are
encoded here and in the ledger. They are **fixed** — derived from "a bug is a
frozen assumption meeting reality, and every harness assumption attaches to one
of: input / executor / environment / time / user / judge / self / loop-closure."

---

## The loop's core logic

```
while not converged:
  pick next case            # highest-value unrun axis, OR a pending derived case
                            # (NEVER an owner-only axis/case — see "Owner-only axes")
  run it                    # weakest viable model (M1); LLM-free where the axis allows
  if finding(s):
     triage  -> fix in CoDD core (Generality Gate) -> regression test
     DERIVE  follow-up cases from the finding   # a finding implies sibling cases
     reset   that axis's saturation_counter to 0
  else:
     increment that axis's saturation_counter
converged := every AUTONOMOUS axis.saturation_counter >= K(=2)
             AND no AUTONOMOUS pending_cases with status in {pending, running}
             # owner-only axes/cases (D2) are tracked but excluded — they never gate
```

A finding **spawns new cases**. The real `type: requirements` plural finding
derived: *"are other type vocabularies singular-only?"* and *"do other
doc-discovery sites share it?"*. **The loop cannot converge while derived cases
are pending** — that is exactly what makes it continue "until no problems
occur".

---

## The five iteration steps

### 1. SELECT — pick the next case

- **World-change triggers win first (M3).** When the world changes, jump
  straight to the matching axis regardless of recency:
  - new model released → **D5** (weakest-viable run on the new model)
  - new framework / language / IaC in scope → **D3** (stack rotation)
  - a user reports an issue on a real repo → **D11** (alien-repo zoo)
  - a new external benchmark appears → **D12**
- Otherwise prefer a **`pending_cases` entry** (derived cases are the loop's
  fuel — drain them), highest `priority` first.
- Otherwise pick the **least-recently-run axis** whose `status != saturated`.
  Ties break toward the cheaper / more automatable axis.

### 2. RUN — weakest viable executor (M1) + model economics

Always run the axis with the **weakest model that can plausibly complete the
task**. A weak model is the most *sensitive instrument*: it trips on the
ambiguity a strong model silently papers over. **Dogfood with weak; fix with
strong.**

**Model-economics rule (NON-NEGOTIABLE in this harness).** Run each axis on the
cheapest viable executor:

| Axis kind | Executor | Why |
|-----------|----------|-----|
| deterministic (D8, D10, D11, D14; D7 stub-AI) | **free** — a Python script in `dogfood/scripts/`, zero LLM | the layer is pure; an LLM adds cost and noise |
| LLM input-family (D1, D3, D4, D6, D9) | **cheapest viable model** — codex-xhigh / sonnet | M1: the weak model is the better instrument anyway |
| owner-only (D2) | **owner-supplied input** + cheapest viable model | not autonomously runnable: fabricating the input makes the agent author==solver (see "Owner-only axes"); excluded from autonomous convergence |
| novel-finding triage / fix | **the strongest model** | reserved for *understanding* a new failure and generalizing the fix |

The strongest model is **reserved for triaging novel findings**, not for
running axes. Everything in this directory makes **zero paid LLM calls**.

### 3. TRIAGE — the rule that perpetuates the loop

For **every** finding, do **both**:

1. **File a fix — behind the Generality Gate.** The fix must generalize, never
   overfit to the dogfood subject. *"The kakeibo app needs X"* is not a CoDD
   fix; *"the implement step truncates any large output"* is. Each fix ships a
   **regression test** so the finding can never silently return (M2: stubs
   prevent regressions, they never *discover* — discovery only comes from a real
   first-contact run). Use the `codd-improve` Generality Gate.
2. **Derive follow-up cases.** A finding almost always implies new variations: a
   bigger app, the same input on another CLI, a new stack, a harder
   interruption, *the same weakness in a sibling vocabulary*. Append each to
   `pending_cases` with `origin: "derived:<finding-id>"`, and list its id in the
   finding's `derived_case_ids`. These become future SELECT candidates.

Human-judge findings (**D13**) enter through this *same* triage. The demo-day
mobile-nav incident is the canonical example: one human catch → the
environment-coverage axes were born.

### 4. RECORD — update the ledger

- Append the finding(s) to the top-level `findings` list (with `id`, `axis`,
  `date`, `model_used`, `symptom`, `root_cause`, `fix_commit`,
  `regression_test`, `status`, `derived_case_ids`).
- Append derived cases to `pending_cases`; mark a consumed case `done`.
- Update the axis: a **new finding resets** `saturation_counter` to `0` and
  `status` to `active`; a **dry run increments** `saturation_counter` and sets
  `status: saturated` the moment it reaches **K = 2**. Set `last_run`.

`python dogfood/run_iteration.py` does steps 2/4 automatically for the
**automatable axes** (it runs the `run_*.py` scripts, appends any new findings,
updates counters, and prints the convergence report). LLM axes are recorded by
hand (or by the `examples/claude_workflows/codd-dogfood-loop.js` driver).

### 5. REPEAT / EXIT

Keep iterating. **The loop exits only when every axis is saturated AND
`pending_cases` is empty.** Because TRIAGE keeps minting derived cases,
saturation recedes whenever the harness is still imperfect — exactly the "until
problems stop emerging" contract.

---

## Saturation & convergence (precise definitions)

- **K = 2.** An axis is `saturated` only after **2 consecutive finding-free
  runs**. One new finding resets that axis to `saturation_counter: 0`.
- **RESET across axes.** Failures cluster. When a fix touches a **shared stage**
  (extract / implement / verify / propagate), reset every axis that exercises
  it (typically D1, D4, D10, D11, D14) — set their `saturation_counter: 0`. A
  **new axis born** from a finding starts unrun. A **derived case** added to an
  axis blocks that axis's saturation until the case has been run dry.
- **Convergence.** `converged == (∀ AUTONOMOUS axis: status == "saturated") AND
  (no AUTONOMOUS pending_cases with status ∈ {pending, running})`. A moving target
  by design. **Owner-only axes are EXCLUDED** from this requirement: an axis whose
  `automation: owner-only` (currently **D2**) is tracked but never gates autonomous
  convergence and is never auto-selected as the next case — see "Owner-only axes"
  below.

---

## Meta-principles (M1 / M2 / M3)

- **M1 — weakest-viable-model as instrument.** Dogfood with weak models (they
  surface ambiguity); fix with strong models. Encoded as axis **D5** and as
  step 2 of every iteration.
- **M2 — first-contact rule.** Stubs and fixtures only *prevent regressions*;
  they never *discover*. New failure classes come only from genuine first
  contact — a non-builder's hand-written input, an alien repo, a live cross-CLI
  run. Every fix still gets a regression test (that is the stub's only job).
- **M3 — ledger + saturation rotation + world-change triggers.** Rotate to the
  least-recently-exercised axis; saturate at **K = 2** dry runs; but let
  world-change triggers preempt rotation (new model→D5, new framework→D3, user
  issue→D11, new benchmark→D12). This is the same `dependency_freshness`
  mechanism CoDD ships, applied self-similarly to its own QA.

---

## Executor assignment (which model runs which role)

The loop has **model-bearing roles** and **model-free machinery**. This is the
approved assignment, recorded machine-readably in
[`ledger.yaml`](./ledger.yaml)'s `model_roles` block (and per-axis as
`sut_model`). It ties directly to the M1/M2/M3 principles above.

| Role | What it does | Executor |
|------|--------------|----------|
| **Loop machinery** | `run_iteration.py` tick, ledger I/O, convergence math | **none** (deterministic, free) |
| **Automatable axes** | the deterministic axis runners — D7 chaos, D8 adversarial/input-validation, D10 round-trip, D11 alien-repo zoo, D14 self-application | **none** (deterministic, free, CI-runnable forever) |
| **SUT executor** (default) | the `ai_command` CoDD uses to actually build the app/project under test, for the LLM axes D1-D5, D9 | **the cheapest viable model — Sonnet 4.6 is the floor** (Haiku only where it can complete) |
| **SUT executor** (D6) | same, but the cross-CLI axis by definition | **Codex (gpt-5.5 xhigh)** |
| **Iteration driver** | selects the next case, judges whether output is a real finding, derives sibling cases, updates the ledger | **Opus 4.8** |
| **Triage + mechanical fix** | root-cause a **mechanical** finding (a bug patch to existing behaviour), fix CoDD core generically behind the generality gate, write the regression test | **Opus 4.8** (autonomous) |
| **Conceptual-finding design** | when a finding is **conceptual** — the fix is a NEW abstraction or NEW check-class, not a patch — design the new concept, then commit+push it like any other fix | **Fable 5, autonomous** (Tier 3; reroute→Opus, also autonomous; no owner gate) |
| **Portfolio evolution (anti-convergence)** | periodically ask *"which failure class does NO current axis cover?"* and mint NEW axes / check-categories | **Fable 5, autonomous** (Tier 3) |
| **Owner dialogue** | the owner-facing **product-strategy** conversation (novel product direction, not loop-internal fixes) | **Fable 5** |

**SUT floor = Sonnet 4.6 (M1).** A lighter model is the *higher-sensitivity
instrument*: it surfaces harness gaps a stronger model papers over. Empirically,
Sonnet surfaced **all 6** greenfield findings (F-plural-type, F-resume-options,
F-output-fragmentation, F-fence-contamination, F-verify-false-green,
F-verify-exit0). **Fable 5 is explicitly NOT used as the SUT** — it is too
capable; it would hide harness gaps, violating M1.

**Driver & routine triage run on Opus 4.8, not Fable 5.** Fable 5 is kept out of
the *SUT role* and the *routine mechanical tiers* on two independent grounds —
but it is **present at the apex tier** (Tier 3), not outside the loop entirely:

1. **Not as SUT (M1).** Too capable; it would mask the very gaps the loop exists
   to find.
2. **Not for routine mechanical triage.** Opus 4.8 is sufficient for a bug patch
   to existing behaviour, is cheaper, and is classifier-free — see the reroute
   caveat below. So mechanical fixes stay on Opus (Tier 2).

Triage/mechanical-fix on the **strongest practical model (Opus 4.8)** is the
second half of M1: *dogfood with the weak instrument, fix with the strong one.*

**Mechanical vs conceptual findings (the Tier 2 / Tier 3 split).** Triage
classifies every finding:

- **Mechanical** → a bug patch to *existing* CoDD behaviour (e.g. the type
  matcher ignored a plural; verify exited 0 on a red suite). Fixed on **Opus 4.8
  (Tier 2)**, behind the Generality Gate, with a regression test. This is the
  default — the 6 greenfield findings are all mechanical.
- **Conceptual** → the right fix is a **NEW abstraction or a NEW check-class**,
  not a patch. The canonical precedent is the **D13 human/visual-judge** finding
  (mobile nav vanished on one viewport) whose fix was an entirely new
  *environment-coverage* check-axis — and likewise the enablement-coverage axis.
  Conceptual findings are designed by the **apex model (Fable 5)** and then
  **committed+pushed autonomously, exactly like a mechanical fix — there is no
  owner-approval gate (Tier 3)**. The mechanical/conceptual split now only routes
  *which model designs the fix* (Opus for mechanical, Fable for conceptual), not
  whether a human approves it. Mark them `class: conceptual` in the ledger.

**Portfolio evolution (anti-convergence) — also Tier 3.** Convergent
case-derivation (the Opus iteration driver, Tier 2) tends toward *obvious
variants* of known axes. Periodically a divergent pass asks **"which failure
class does NO current axis cover?"** to discover NEW dogfood axes / check
categories. That "what are we NOT testing" judgment is the apex role
(Fable 5, autonomous — the owner is informed, not a gate), not a
convergent-derivation job.

**Classifier-reroute caveat → Opus, still autonomous (Tier 3 is best-effort).**
The loop's content — cross-model testing, adversarial / input-validation cases,
security-adjacent codebases — can trip Fable 5's content-safety classifiers and
reroute to Opus 4.8. For the routine tiers we assign Opus *directly* to avoid
that reroute churn. For Tier 3 a rerouted finding is simply **designed and fixed
by Opus 4.8 autonomously** — it is NOT parked in an owner queue. The owner
receives no per-finding approval request and no required feedback: **loop
findings, mechanical AND conceptual, resolve autonomously** (designed by the
apex model, committed+pushed like any other fix). M1 routes the SUT to the weak
instrument; M2 (first-contact) is what surfaces the conceptual gaps in the first
place; M3's world-change triggers are the same anti-convergence reflex the
portfolio-evolution pass formalizes.

The whole 4-tier stack is the owner's **"the apex model designs, the execution
model executes"** thesis applied recursively to the QA loop: execution (build /
mechanical fix / run) = Sonnet + Opus; design-level work (new concepts /
portfolio blind-spots) = Fable. The owner gates none of the loop-internal
work — only genuinely novel PRODUCT direction (below) stays the owner's domain.

### Cost tiers

- **Tier 0 — free / continuous.** Loop machinery + the deterministic axes
  (D7, D8, D10, D11, D14). Zero LLM calls; runs forever in CI.
- **Tier 1 — cheap / frequent.** The SUT runs for the LLM axes — Sonnet 4.6
  (floor) for D1-D5/D9, Codex (gpt-5.5 xhigh) for D6.
- **Tier 2 — strong / episodic.** Opus 4.8 for the iteration driver and for
  **mechanical** triage/fix — invoked **only when a finding needs judgment,
  triage, or a bug patch**, never continuously.
- **Tier 3 — apex / rare.** Fable 5 (autonomous) for **conceptual** findings (the
  fix is a new abstraction / new check-class) and for the **portfolio-evolution**
  anti-convergence pass (minting new axes). The fix is committed+pushed like any
  other — **no owner-approval gate**. Best-effort: the loop's content can trip
  Fable 5's content-safety classifiers and reroute to Opus 4.8, in which case the
  finding is **designed and fixed by Opus autonomously** (not parked for the
  owner). Loop findings — mechanical AND conceptual — are resolved autonomously;
  only genuinely novel PRODUCT direction stays the owner's domain.

---

## What dogfood CANNOT find (honest limits — the owner's domain)

This loop hardens the harness against *recurring, observable* failure classes,
and now resolves **every** loop finding (mechanical AND conceptual) autonomously.
What remains the owner's domain is not loop-internal fixes but genuinely novel
*product* direction — it is structurally blind to three things, and a green axis
does not imply these are handled:

- **New-concept divergence.** When CoDD invents a genuinely new concept, LLMs
  converge to their training mode and the design *diverges* from intent. Keeping
  that divergence productive is a human judgment call, not a dogfood signal.
- **Taste.** "Technically correct but wrong" — naming, ergonomics, whether the
  abstraction is the *right* one. D13 catches *some* by eye, but taste is not
  enumerable.
- **The self-hosting limit.** CoDD cannot fully dogfood its *own* development —
  that is exactly the new-concept + forgetting regime that needs human judgment.
  D14 runs only codd's *read-only* checks on codd-dev; it does not self-build.

When a finding turns out to be one of these, mark it `wontfix` with a note — it
belongs to human judgment, not the loop.

### Owner-only axes (the author==solver collapse) — D2

One axis is **structurally not autonomously-runnable**, distinct from "manual".
A `manual` LLM axis (D1, D3, D4, D6, D9) is still autonomous: the agent supplies
a genuine *third-party-style* input (a non-builder's spec, an alien stack, a
bigger app) it did not also design the solver for. **D2 (messy-human
requirements)** is different. D2 verifies the gap between a *real human's*
raw/under-specified requirements doc and CoDD's elicit / open-questions
machinery. If an autonomous agent **fabricates** the "raw human requirements,"
the agent becomes **both author and solver** — the gap D2 tests collapses and the
run is theater (a self-made problem, self-solved). That is exactly the
divergent/human domain above ("what dogfood cannot find").

So D2 is marked **`automation: owner-only`** (and `status: owner-only`) in the
ledger. It **fires only when the OWNER supplies a genuine requirements doc**; an
autonomous agent must never synthesize one to "run D2." Consequences in the loop:

- **Convergence excludes owner-only axes.** `converged == every AUTONOMOUS axis
  saturated AND no AUTONOMOUS pending case open`. Owner-only axes and their
  `pending_cases` are **tracked but never gate** — otherwise the autonomous loop
  could never converge while waiting on input it is forbidden to invent.
- **Selection skips owner-only.** The "next case" picker (both
  `run_iteration.py` and the `codd-dogfood-loop.js` driver) never auto-selects an
  owner-only axis or an owner-only pending case (their status is `owner-only`, not
  `pending`/`running`).
- `run_iteration.py` reports owner-only axes/cases on a separate line so they
  stay visible without blocking the `converged:` verdict.

---

## Axis → runner map

| Axis | Name | Runner | Cost | Auto |
|------|------|--------|------|------|
| D1 | first-contact / dialect | manual: *First-contact protocol* | llm | manual |
| D2 | messy-human requirements | **owner-supplied** input only: *Messy-requirements protocol* | llm | owner-only |
| D3 | stack rotation | manual: *Stack-rotation protocol* | llm | manual |
| D4 | complexity ladder | manual: *Complexity-ladder protocol* | llm | manual |
| D5 | weakest-viable-model | modifier: *Weakest-viable-model protocol* | cheap | manual |
| D6 | cross-CLI live run | manual: *Cross-CLI protocol* | llm | manual |
| D7 | chaos / interruption | `python dogfood/scripts/run_d7_chaos.py` | free | scripted |
| D8 | adversarial content | `python dogfood/scripts/run_d8_adversarial.py` | free | scripted |
| D9 | lifecycle evolution | manual: *Lifecycle protocol* | llm | manual |
| D10 | round-trip fidelity | `python dogfood/scripts/run_d10_roundtrip.py` | free | scripted |
| D11 | alien-repo zoo | `python dogfood/scripts/run_d11_zoo.py` | free | scripted |
| D12 | external benchmarks | ci/external: SWE-bench harness | llm | ci |
| D13 | human-judge / visual | manual: *Human-judge protocol* | human | manual |
| D14 | self-application | `python dogfood/scripts/run_d14_self.py` | free | scripted |

The loop tick `python dogfood/run_iteration.py` runs **all** the scripted axes
(D7, D8, D10, D11, D14) in one pass and prints the convergence report.

### Manual protocols (LLM axes — run on the cheapest viable model)

- **First-contact protocol (D1).** Hand a *non-builder's* plain-language spec
  (their dialect, loose type names, prose) to `codd greenfield` on the weakest
  viable model. Do not pre-clean the input — the mess is the test.
- **Messy-requirements protocol (D2) — OWNER-ONLY.** Feed a deliberately
  contradictory / duplicated / under-specified requirements doc; watch
  elicitation, dedup, and whether CoDD *questions the gaps* rather than guessing.
  **The requirements doc MUST be owner-supplied — an autonomous agent must not
  fabricate it** (doing so makes the agent both author and solver and collapses
  the gap this axis tests; see "Owner-only axes" above). Excluded from autonomous
  convergence; fires only when the owner provides a genuine doc.
- **Stack-rotation protocol (D3).** Choose a language + framework + IaC the
  harness has **not** seen recently; `codd greenfield --language <lang>`; record
  stack-specific assumptions that leak.
- **Complexity-ladder protocol (D4).** Re-run greenfield on the next rung up the
  size ladder. Findings are usually ceilings (token, time, memory).
- **Weakest-viable-model protocol (D5, modifier).** Set `ai_command` to the
  weakest model that can plausibly finish, then run an input-family axis.
- **Cross-CLI protocol (D6).** Set `ai_command` to a different live CLI (codex,
  etc.) and re-run a known greenfield input end-to-end.
- **Lifecycle protocol (D9).** Apply a sequence of `codd-evolve` / `codd fix`
  changes to a living generated app; after each, assert coherence
  (`codd validate` / `codd doctor` / `codd diff`).
- **Human-judge protocol (D13).** A person reviews a generated/evolved app —
  especially visuals and navigation across devices. Any defect is filed and
  triaged like any other.

### Scripted runners (free, no LLM, CI-safe)

Each prints a summary and **exits non-zero on any crash** (a crash *is* a
finding). They make **no LLM calls** and **degrade gracefully offline**.

- `run_d14_self.py` (**D14**) — runs codd's own read-only gates on codd-dev
  (DAG `dag verify` red-check + config-key validation via the public
  `codd.config_schema` / `codd.dag.runner` APIs). Nonzero exit on a regression
  (a check crashing / raising).
- `run_d11_zoo.py` (**D11**) — iterates the repos in `dogfood/zoo.yaml`; for
  each available repo runs only the DETERMINISTIC layers (`extract_facts`,
  `derive_iac_nfrs`, `codd.frontmatter`, `build_restoration_report`) and records
  crashes/exceptions as findings. A repo path/URL unavailable offline is
  **SKIPPED with a note**. Ships 3 local synthetic fixtures so it runs offline
  and free; add real OSS repos by path or URL in `zoo.yaml`.
- `run_d10_roundtrip.py` (**D10**) — on a fixture project with design docs: runs
  deterministic extract, builds the restoration/coverage view, compares the
  recovered node inventory against the originals, and reports coverage drops as
  findings.
- `run_d8_adversarial.py` (**D8**) — feeds hostile inputs (path-traversal
  filenames, fenced prompt-injection markdown, malformed/huge frontmatter,
  binary garbage) into the extract/frontmatter/parsing layers and asserts
  fail-closed: no crash, no write outside the tree. Findings = any crash or
  escape.
- `run_d7_chaos.py` (**D7**) — stub-AI greenfield (reusing the
  `tests/greenfield/conftest.py` pattern): kills mid-stage at randomized points,
  resumes, and asserts convergence + option restoration. No real LLM.

---

## Quickstart

```bash
# one loop tick — runs every automatable axis, updates the ledger, prints the
# convergence report, and exits nonzero if a NEW finding appeared:
python dogfood/run_iteration.py

# or run a single axis:
python dogfood/scripts/run_d14_self.py
python dogfood/scripts/run_d8_adversarial.py
python dogfood/scripts/run_d11_zoo.py      # offline-safe (local fixtures)
python dogfood/scripts/run_d10_roundtrip.py
python dogfood/scripts/run_d7_chaos.py

# then record LLM-axis runs by hand, drain pending_cases, and repeat until the
# convergence report says: converged: true
```

The full-loop driver including the LLM axes is
[`examples/claude_workflows/codd-dogfood-loop.js`](../examples/claude_workflows/codd-dogfood-loop.js)
— run it repeatedly until the convergence report says converged.
