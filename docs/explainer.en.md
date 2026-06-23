> 🌐 [日本語](explainer.md) · **English** · [简体中文](explainer.zh.md)

# CoDD (Coherence-Driven Development) Explained

**Target audience**: SEs, PMs, SIer managers, and engineers interested in AI-driven development
**Assumed background**: Programming experience. Familiarity with terms such as V-model, DI, and ORM.

---

## The Problem CoDD Aims to Solve

In large enterprise systems (tens of millions of lines of code), **when you change one part, which other parts are affected?**

### Why Is This Hard?

In an ordinary system, IDE "find references" or grep is enough to trace the impact. But in enterprise packaged software, there are dependencies beyond what the code shows.

**What you can trace by reading code:**
- File A imports File B
- Function A calls Function B
- Class A inherits from Class B

**What you cannot trace by reading code:**
- The call target changes at runtime based on configuration file values (Spring DI, plugin mechanisms)
- Master table values control business logic branches (the code only has a SELECT statement)
- Implicit rules like "if you change this, you must also change that" (written nowhere — lives only in the heads of experienced engineers)

### How It Is Handled Today

A 20-year veteran engineer solves this with the **mental model in their head**. They intuitively know things like "touching this will involve that master table" or "in production it's only Stripe, so PayPal just needs a check." This is pure intuition.

### What the Problem Is

When that person leaves, **no one can safely touch the system anymore**. Junior engineers cannot see the dependencies, so they forget to update things and cause incidents.

### What CoDD Aims For

**Reproduce 70% of the veteran's mental model with AI, so junior engineers can also make changes safely.**

It doesn't need to be perfect. Just surfacing "if you change this, also look here" is transformative.

---

## What CoDD's Name Means

**CoDD = Coherence-Driven Development**

### The Traditional Approach (SDD = Spec-Driven Development)

```
Requirements → High-level Design → Detailed Design → Code → Tests
```

A one-way ticket. Flow goes only top to bottom. When you're writing code and realize "this design won't work," the requirements document does not automatically update. A human must notice it and fix it by hand.

### The CoDD Approach

```
Requirements ⇄ High-level Design ⇄ Detailed Design ⇄ Code ⇄ Tests
```

Bidirectional. When you touch any part, the impact propagates both up and down automatically.

- Fix code → propagates "this is also affected" all the way up to Detailed Design → High-level Design → Requirements
- Add a requirement → propagates "this needs to change" all the way down to High-level Design → Detailed Design → Code → Tests
- Fix a design document midway → propagates both up and down

**It is not the spec that drives. It is the coherence of the design.** That is why it is called Coherence-Driven.

---

## Another Meaning of Coherence: Structurally Preventing "Looks Like It Works" (False Greens)

"Coherence = bidirectional change propagation" is the first half of the story. As CoDD has evolved, another meaning has emerged — and it is arguably the most important one globally.

**Whether the artifacts truly satisfy the requirements.**

### The Biggest Blind Spot in AI Coding: "Tests Are Green = Done" Is Broken

Today's AI code generators (agents) work like this: write code → run tests → green → "done."
But that "green" cannot be trusted. In 2026, the industry's own data showed exactly that.

- About **19.78% of "resolved" results on SWE-bench Verified are semantically incorrect** — they merely happened to pass the test, or reward-hacked the evaluation system.
- UC Berkeley demonstrated that **all eight major agent benchmarks can be reward-hacked to ~100%**.
- OpenAI **withdrew SWE-bench Verified** — an audit found that 59.4% of the problem test suites were themselves broken (broken ground truth).

In short, **"looks like it works" = false-green** is rampant. Generation has become a commodity, and the real difficulty has shifted to **"verifying that the generated artifact actually satisfies the requirements."**

### CoDD's Answer: If It Cannot Be Proven, Don't Make It Green

The CoDD loop closes at Requirements → Design → Tests → Implementation → **verify**. The final verify step uses the dependency and contract graph to **mechanically determine whether the implementation truly satisfies the contracts of the requirements and design**. **Anything that cannot be proven to satisfy them is not turned green (anti-false-green).**

This goes beyond "code consistency" — it is **"requirement-satisfaction coherence"**, the deeper meaning of Coherence.

Concrete examples of "passes but should not" that CoDD catches:

- A required feature **reads** a data item that **no one writes** (no write path exists in the design) → the feature is physically impossible to execute, yet conventional tools show green. CoDD detects this.
- A feature like "notify individually after N days of inactivity" references a recipient ID, but the process that generates it is absent from the design → individual notifications will never arrive, yet the result is green. CoDD turns it red.

"Tests passed" is not enough. **"The contracts among artifacts are closed such that the requirements are satisfied"** — that is CoDD's Coherence.

### Contract Kernel: A Core That Is Independent of Language and Framework

This verification does not depend on knowledge of any specific language or framework. CoDD's core (Contract Kernel) holds no language names, framework names, or project-specific vocabulary — it looks only at **normalized "contracts" and "dependencies (produce/consume)"**. Adapters and profiles outside the core handle language- and framework-specific concerns. The same verification philosophy therefore works on any stack.

### Why This Works Now

The world's tools (GitHub Spec Kit, various coding agents, Copilot / Cursor) are moving in the direction of **improving generation** — making it faster and more autonomous from a spec. CoDD bets on the next step: **"structurally proving that what was written satisfies the requirements — or, if it cannot, refusing to show green."** Now that the industry is finally recognizing "false-green / verification gap" as a problem, CoDD was designed from the start for exactly that gap.

---

## The Philosophy of CoDD: Inventing Nothing New

CoDD invents not a single new concept.

Software development has established best practices accumulated over decades: the V-model, UML (class diagrams, sequence diagrams, ER diagrams, CRUD diagrams), dependency management, change management, and regression test planning. All of it is in the textbooks. **Everyone already knew** the right way.

But no one could execute it.

- Writing detailed design (class diagrams, sequence diagrams, ER diagrams) for every use case → **takes weeks**
- Keeping design documents updated after every requirements change → **too tedious; left to rot**
- Tracking impact scope accurately → **exists only in the veteran's head**
- Maintaining consistency across all artifacts on every change → **humans manually check everything**

The problem was never a lack of knowledge. **The problem was the cost of execution.**

SIers have not been skipping detailed design because it was unnecessary. It was simply too expensive for humans to do. Now that AI has driven the execution cost close to zero, **you can just do the right process in full**.

What CoDD does:

| Practice | Origin | How CoDD Uses It |
|----------|--------|-----------------|
| Config merging | Rails / nginx / ESLint | Two-layer configuration architecture |
| V-model phases | Waterfall | Wave dependency graph |
| Detailed design (UML diagrams) | V-model detailed design | AI auto-generates from high-level design |
| Dependency management between artifacts | Makefile (1976) | Automatic generation-order control |
| Front matter | Jekyll / Hugo | Self-describing metadata on artifacts |
| DAG | Airflow / Make | Execution-order control via dependency graph |
| Training loop | Deep Learning | Generate → Review → Improve loop |

Engineers across the world independently converged on the same patterns — that is evidence these are correct. The fact that no wheel is being reinvented is the basis for trust.

And every artifact is tagged with **dependency metadata**. This metadata is what enables Change Propagation. When a requirement changes, the dependency graph is traversed to automatically identify the scope of impact, and only the affected artifacts are regenerated. Humans miss things. AI does not. AI does not tire. AI can redo this any number of times.

**This was fundamentally beyond human capability. It is work for AI.**

### In One Sentence

Do all the development phases written in IPA's Common Frame (SLCP) without skipping any. Everything that a tech lead or PM should be doing — AI does it all. The only addition is **tagging every artifact with dependency metadata**. That's it.

With that metadata, Change Propagation works. When a requirement changes, the dependency graph is traversed to automatically identify impact scope, and only the affected artifacts are regenerated. Without the metadata it is just an artifact-generation tool. With the metadata, it is CoDD.

---

## The Three Pillars of CoDD

### Pillar 1: Automatically Generate Design from Requirements

Feed requirements (functional + non-functional) to AI, and it presents several design patterns.

```
Input: "Build a payment feature. 1M transactions per month. PCI DSS compliant."

AI output:
  Pattern A: Microservices split (scalability ○, complexity △)
  Pattern B: Modular monolith (simple ○, scale △)
  Pattern C: Event-driven (loose coupling ○, debugging △)

→ Human chooses (Human-in-the-Loop)
→ Based on the chosen pattern, design documents, code skeletons, and test skeletons are generated
```

AI does not decide on its own. It presents candidates, and a human chooses.

### Pillar 2: Manage Dependencies and Propagate Changes (★ The Core)

Manage the dependencies of all V-model nodes (requirements, design, code, configuration, DB, tests) as a graph. When any part is changed, traverse the graph to automatically surface "this is also affected."

```
stripe_adapter.py was changed
  → Affects processor.py (code dependency)
  → paypal_adapter.py should also be updated (convention dependency)
  → Verify consistency with payment_fees master (data dependency)
  → Update design document (documentation dependency)
  → Add test cases (test dependency)
```

Rather than a human tracing this in their head, the dependency graph produces this automatically.

### Pillar 3: Context Management (What to Feed the AI)

When letting AI work, **what to include in the context** is critically important.

- Include all source → token count overflow, or the middle sections are forgotten (Lost in the Middle problem)
- Include too little → impacted areas are missed

CoDD uses the dependency graph to dynamically select only the information needed for the current change and pass it to the AI. Moreover, it varies the resolution — full text / summary / metadata only — based on importance.

### Relationship Between the Three Pillars

```
Pillar 1: Requirements → Generate design candidates → Human chooses
Pillar 2: Automatically propagate changes bidirectionally via dependency graph  ★ The core
Pillar 3: Select only the necessary information from the dependency graph and pass it to AI
```

Pillar 2 is the core. Pillars 1 and 3 only work because Pillar 2's dependency graph exists.

---

## What Is a Dependency Graph?

### The Basics

A graph in the graph-theory sense. Made of **nodes (vertices)** and **edges (lines)**.

```
[A] ---calls--→ [B] ---reads--→ [C]
```

- Node = a thing (file, function, config key, DB table, section of a design document, etc.)
- Edge = a relationship between things (A calls B, B reads C, etc.)

### An Ordinary Dependency Graph (Existing Technology)

This is the same thing an IDE's "find references" does. Nodes are source code elements. Edges are code-level relationships such as `imports`, `calls`, and `extends`. These can be auto-detected via AST (Abstract Syntax Tree) analysis or LSP (Language Server Protocol).

### CoDD's Dependency Graph: What Is Different?

An ordinary dependency graph stays inside the code. CoDD **mixes in things outside the code**.

**More types of nodes:**
```
Code:          processor.py, stripe_adapter.py, charge()
Configuration: payment.provider key in config.yaml
DB table:      payment_fees master
Design doc:    Section 3 of payment_flow.md
Test:          test_processor.py
Batch:         nightly_settlement.py (nightly batch)
Implicit rule: Convention "adapters must share the same interface"
```

Heterogeneous things live on the same graph. Code, DB, design docs, tests, and tacit knowledge are all connected by edges in a single graph.

**More types of edges:**
```
imports      — imports a file (auto-detected)
calls        — calls a function (auto-detected)
reads_config — reads a config value (auto-detectable)
switched_by  — call target switches on a config value (LLM inference or framework analysis)
driven_by    — master data controls behavior (LLM inference + human annotation)
convention   — should be changed together by convention (human input or git history)
```

The top three can be obtained mechanically. The bottom three cannot be understood just by reading the code. **This is CoDD's frontier.**

### Concrete Example: Payment Processing Module

```
[processor.py::route_payment]
    │
    ├── reads_config ──→ [config.yaml::payment.provider]
    │                        │
    │                        ├── switched_by ──→ [stripe_adapter.py]
    │                        │                    (when provider=stripe)
    │                        │
    │                        └── switched_by ──→ [paypal_adapter.py]
    │                                            (when provider=paypal)
    │
    ├── driven_by ────→ [DB: payment_fees master]
    │                    (fee rate controls logic)
    │
    └── implements ───→ [Design doc: payment_flow.md Section 3]

[stripe_adapter.py]
    │
    └── convention ───→ [paypal_adapter.py]
                        (convention to maintain the same interface)
```

This is a visualization of "the veteran engineer's mental model." A 20-year veteran knows this structure implicitly. CoDD makes it an explicit data structure.

---

## Q&A: Common Questions About the Dependency Graph

### Q: How is the data stored? YAML files? A DB?

**It depends on the stage.**

| Stage | Storage format | Reason |
|-------|----------------|--------|
| Initial (small–medium scale) | YAML + SQLite | Human-writable, diffable in git, lightweight |
| When it grows large | SQLite (primary) + Neo4j (for search) | With tens of thousands of nodes and hundreds of thousands of edges, YAML is no longer viable |

**YAML example:**
```yaml
nodes:
  - id: src/payment/processor.py::route_payment
    type: method
    description: "Payment routing"

  - id: config/payment.yaml::provider
    type: config_key
    possible_values: [stripe, paypal, internal]

edges:
  - from: src/payment/processor.py::route_payment
    to: config/payment.yaml::provider
    type: reads_config
    confidence: 1.0
    source: auto_detected

  - from: config/payment.yaml::provider
    to: src/payment/stripe_adapter.py
    type: switched_by
    confidence: 0.95
    condition: "provider == 'stripe'"
    source: llm_inferred
```

The advantages of YAML are that it is human-readable, human-writable, human-editable, and diffable in git. The limit of YAML is that search becomes slow when the node count exceeds around 10,000.

For small-to-medium projects, start with YAML alone and migrate to SQLite as the project grows — that is sufficient.

Summary of how to use each:
```
YAML   ──→ Human-authored annotations, convention rules, manual definitions (entry point)
SQLite ──→ The graph itself. Where machines search at speed (primary store)
Neo4j  ───→ Path search and visualization. Added only for large scale (optional)
JSON   ────→ Handoff to AI agents (lightweight slices)
```

---

### Q: What are the "confidence" and "condition" attached to edges?

CoDD edges are not just lines. They carry three important attributes.

**Attribute 1: Confidence — how much can this edge be trusted?**

```
imports (file is imported)                  → 1.0  (certain)
calls (function is called)                  → 1.0  (certain)
switched_by (call target switches on config)→ 0.95 (nearly certain)
driven_by (master data controls behavior)   → 0.8  (fairly confident)
convention (should be changed together)     → 0.7  (probably)
```

Things that can be auto-detected have high confidence. The more an edge relies on human knowledge or LLM inference, the lower the confidence.

**Attribute 2: Evidence source (Evidence Ledger) — why can we say this edge exists?**

A single edge can have multiple pieces of evidence.

```
Convention edge: stripe_adapter → paypal_adapter:
  Evidence 1: Co-changed in 11 of 12 commits in git history (history, score: 0.75)
  Evidence 2: PR review comment "align the interface" (human, score: 0.90)
  Evidence 3: LLM inferred "because they share the same interface" (inferred, score: 0.60)
```

Confidence is computed from the evidence ledger — it is not hard-coded but is recalculable from the underlying evidence. When multiple weak pieces of evidence accumulate, confidence rises (Noisy-OR mechanism). This models the veteran's intuition: "several signals align, so something's off."

**Attribute 3: Validity condition (condition predicate) — when is this edge active?**

```yaml
- from: config/payment.yaml::provider
  to: src/payment/stripe_adapter.py
  type: switched_by
  condition: "provider == 'stripe'"
```

In production, `provider=stripe`, so this edge is active. But in a development environment it might be `provider=internal`. In that case, this edge is irrelevant for that environment.

Without conditions, dependencies from all environments mix together, causing an explosion of noise ("this is affected, even though it has nothing to do with production"). Experienced engineers filter by condition in their heads. CoDD makes that filtering explicit as data.

---

### Q: How is the dependency graph built? Do you write it by hand?

**It is built incrementally and automatically. Writing everything by hand is impossible.**

#### Phase 1: Automatically collect what machines can find (minutes to tens of minutes)

Analyze code mechanically with an AST analysis tool (such as Tree-sitter).

```python
# Analyzing this code:
from stripe_adapter import StripeAdapter

def route_payment(amount):
    provider = get_config('payment.provider')
    if provider == 'stripe':
        return StripeAdapter().charge(amount)
```

```
Auto-detected:
  processor.py ── imports ──→ stripe_adapter.py        (confidence: 1.0)
  route_payment ── calls ──→ StripeAdapter.charge      (confidence: 1.0)
  route_payment ── reads_config ──→ payment.provider   (confidence: 1.0)
```

No humans or LLMs required. The AST parser simply parses the syntax. **This alone covers 60–70% of dependencies.**

#### Phase 2: Framework analysis + LLM inference (hours)

Use the `reads_config` edges from Phase 1 ("reads a config value") as a starting point to dig **further**.

**Ask the framework first (Spring Boot example):**

The framework-standard mechanism of Spring Boot Actuator can reveal what is wired to what via DI.

```
GET /actuator/beans → Returns a full list of registered beans and their dependencies
GET /actuator/conditions → Returns evaluation results for @Profile and @ConditionalOnProperty
```

This lets you obtain the config-to-implementation mapping without using LLMs. In the original design this point was overlooked and described as "LLM inference is the only option," but in fact querying the framework can retrieve quite a lot.

**Use LLM only for what the framework cannot provide:**

Custom factories, proprietary registry patterns, and similar things that cannot be analyzed mechanically are fed to an LLM to produce candidates. However, what the LLM does is not "imagine dependencies" but "classify patterns and suggest candidates."

**LLM is the last resort.** Machine-obtainable → framework-obtainable → LLM only for what remains unclear. In that order.

#### Phase 3: Confirm with humans (ongoing)

Show LLM inference results to experienced engineers to extract their knowledge.

```
CoDD:   "We inferred that stripe_adapter and paypal_adapter
         are switched by config.payment.provider.
         Is that correct?"

Veteran: "That's right. There's also an internal_adapter.
          And these three must always keep the same interface."

→ Add node + add convention edge
```

If you ask "please write down all the dependencies" from scratch, no one will do it. But "is this right? Is there anything else?" is answerable. Use LLM inference as seeds to surface human tacit knowledge.

#### Phase 4: Learn from history (ongoing, automatic)

| Data source | What it reveals | Edge generated |
|-------------|-----------------|----------------|
| git log | Files that are co-changed | co_changed_with |
| Incident tickets | "Changed X but forgot Y, caused an incident" | incident_correlated_with |
| Code review | "This also needs fixing" comments | convention_requires |
| Test failure logs | Changing X broke Y's test | validated_by_test |

The longer the system is in use, the smarter the dependency graph becomes. Even a weak mental model at the start will approach a veteran's level over time.

---

### Q: What if SQL is dynamically generated?

An enterprise common scenario. SQL is assembled dynamically through string concatenation or ORM method chaining.

```java
// Traceable by static analysis
String sql = "SELECT fee_rate FROM payment_fees WHERE provider_code = ?";

// Not traceable by static analysis
String sql = "SELECT " + columnName + " FROM " + tableName;

// Even less traceable
StringBuilder sql = new StringBuilder("SELECT fee_rate FROM payment_fees");
if (hasDiscount) {
    sql.append(" JOIN discount_master ON ...");
}
```

**Handle it in three tiers:**

**Tier 1: Collect what can be obtained statically.** Straightforward SQL strings, ORM annotations (e.g., `@Query`), Hibernate and MyBatis mapping definitions. This covers 60–70%.

**Tier 2: Observe at runtime (runtime observation).** Dynamic SQL becomes definite once executed. Capturing DB traces with OTel (OpenTelemetry) records "which table and which columns were actually read." Observation is more reliable than inference.

```
Example trace record:
  span: db-query
    sql: "SELECT fee_rate FROM payment_fees
          JOIN discount_master ON ...
          WHERE provider_code = 'stripe'"
    tables: [payment_fees, discount_master]  ← discount_master found!
```

**Tier 3: LLM + humans.** For special cases that still cannot be captured (e.g., a batch that runs only at year-end), have the LLM analyze patterns and confirm with experienced engineers.

Rough real-world proportions:
```
Captured by static analysis:     60–70%
Captured by runtime observation:  15–20%
Captured by LLM inference:         5–10%
Taught by humans:                  5–10%
Cannot be captured:               a few %  (unobserved rare paths)
```

100% is impossible. But 90% is more than sufficient in practice.

---

### Q: How are runtime observation results fed back into the dependency graph?

Instrument the application with OTel (OpenTelemetry). Traces are recorded as the application runs normally.

```
Production / staging environment (running normally)
    │
    │  A trace is recorded on every execution:
    │  "Which function issued which SQL,
    │   read which tables, then called what next"
    │
    ▼
Feedback pipeline (nightly batch, etc.)
    │
    │  Analyze trace data and reflect in the dependency graph:
    │  - Known edges → add evidence, increase confidence
    │  - Unknown edges → create new (source: dynamic)
    │
    ▼
Dependency graph is updated
```

Concrete example: static analysis detected only the `payment_fees` table. But runtime observation revealed that `discount_master` is also JOINed.

```
Dependency graph before (static analysis only):
  StripeAdapter.charge → payment_fees     ← already known

Dependency graph after (post runtime observation):
  StripeAdapter.charge → payment_fees     ← already known
  StripeAdapter.charge → discount_master  ← ★ newly discovered!
```

The next time anyone changes StripeAdapter, `discount_master` will also appear in the impact list.

No special effort is required. Just instrument the application with OTel and let it run. The more observations accumulate, the more evidence builds up and the higher the confidence climbs.

---

### Q: Do you have to build the graph for the full source before making any changes? Does that mean it can't be used for new development?

**The usage pattern differs between new development and maintenance.**

| Phase | How CoDD is used |
|-------|-----------------|
| New development (greenfield) | Pillar 1 (design generation) is the star. Dependency graph accumulates in CI in the background |
| Growth phase (features increasing) | Dependency graph starts to pay off. Propagation engine engaged |
| Maintenance and changes | **CoDD at its best.** All features running at full capacity |

When running Agile in rapid cycles during new development, the code changes every day, so checking the dependency graph is not particularly useful. However, **Phase 1 (static analysis) is embedded in CI**. The graph updates automatically on every commit. It accumulates in the background even if unused.

Once v1.0 stabilizes, the dependency graph starts to carry weight. When operations and maintenance begin, use it to the fullest.

**The core of CoDD is "a tool for maintenance."** The original question — "in a large enterprise system, if I change this, what will break?" — is fundamentally a maintenance-phase question, so it is natural that CoDD is most powerful there.

### Q: Doesn't the usage pattern differ by scenario?

It does. Usage varies across four scenarios.

**Scenario 1: New development**
- Pillar 1 (design generation) is the star. Dependency graph accumulates in CI in the background.
- Looking at the dependency graph on Day 1 is pointless. It is seeds sown for the future.

**Scenario 2: Legacy system maintenance**
- Where CoDD is most effective. Prevents overlooking "if I change this, what else will break?"
- Top priority is running Phase 3 (knowledge extraction) before the veteran engineers leave.
- Do not try to cover everything at once. Start from areas with high change frequency × high incident frequency.

**Scenario 3: Modernization (legacy → modern migration)**
- The dependency graph makes it possible to plan the migration — "what can be decoupled first?"
- Prioritize managing edges for shared DBs and shared APIs between old and new.
- Works well with the Strangler Fig pattern. When the number of edges pointing to an old node reaches zero, the node can safely be removed.

**Scenario 4: Enhancements to existing systems (★ the most common case)**
- "Adding a new feature" itself is not hard. "Guaranteeing that existing behavior doesn't break" is hard.
- CoDD's value lies in **impact analysis before design**. Rather than breaking things after building, you can compare options at the design stage.
- Compare the impact scope (number of Amber-band items) of Design Option A vs. Option B quantitatively from the graph.

Enhancement-specific usage:

| Usage | Maintenance | Enhancement |
|-------|-------------|-------------|
| Impact analysis | "What broke?" after the change | "What is likely to break?" before the change |
| Design decision | Not needed (just fix it) | Compare impact scope of Option A vs. Option B |
| Regression test | Test the broken areas | Auto-extract impacted tests from dependency graph |

Also effective for regression test planning:
```
Traditional: "Run all tests to be safe" → 3 days
CoDD: Auto-extract only tests impacted → 50 Amber-band + 30 Gray-band
      500 unaffected tests can be skipped → 3 days becomes half a day
```

### Q: Is CoDD useful before it has matured (right after introduction)?

No magic happens on Day 1. The growth curve looks like this:

```
Day 1:    Phase 1 only. Not much different from grep + IDE search. But structured as a graph.
Week 1:   Phase 2 also running. Config-driven dependencies become visible. Beginning to surpass grep.
Month 1:  Phase 3 adds veteran knowledge. Conventions and tacit knowledge are in the graph.
Month 3:  Phase 4 cycles begin. Practical as a "weak mental model."
```

Even before it has matured, embedding it in CI means it grows on its own. Everyday work — commits, reviews, tests, incident response — all becomes data that grows the graph.

The biggest risk is "panicking after the veteran leaves." Even before it matures, Phase 3 (confirming with humans) should be run early. Once they leave, you can no longer ask.

---

## Change Propagation: What Happens When a Change Is Made?

### Basic Mechanics

```
1. A change is introduced (e.g., signature of charge() is changed)
2. From the change point, traverse edges to neighboring nodes
3. From each neighboring node, continue traversing
4. With each traversal, the impact score decreases
5. When the score falls below a threshold, traversal stops
6. All traversed nodes are output as the "impact list"
```

Think of ripples spreading when a stone is dropped in water. Ripples radiate from the point of change and weaken with distance.

### The Pattern of Propagation Depends on What Was Changed

Even changes to the same file produce entirely different propagation patterns depending on their type.

| Type of change | Spread of ripple | Reason |
|----------------|-----------------|--------|
| Public method signature change | Wide | All callers are affected |
| Internal implementation change only | Narrow | External behavior is unchanged |
| Refactoring (no semantic change) | Almost no propagation | Just renamed |
| Config default value change | Conditionally wide | Only environments using that config |
| Semantic change to master data | Most dangerous | Changes logic branching |
| Documentation-only edit | No propagation | No impact on code |

Propagating uniformly regardless of change type would produce noise that makes the system useless.

### Stopping Propagation

Allowing ripples to spread infinitely would result in "everything is affected," which is meaningless. There are five criteria for stopping.

```
① Stop at contract boundaries (highest priority)
   If the public API contract itself has not changed, propagation does not cross to the outside.

② Condition is not satisfied
   Production uses only Stripe → PayPal dependency does not propagate in the production environment.

③ No new information is added
   Stop when only the same type of impact would be produced.

④ Risk score is too low
   Score decreases with distance.

⑤ Depth limit (last safety valve)
```

### Presenting the Impact List: Three-Band Classification

Divide the impact list into three bands by severity.

**Green (safe to auto-update):**
```
1. [auto] test_processor.py — follow the argument change
2. [auto] docs/api/payment.md — update API signature
```
Extremely high confidence. The correct answer is determined mechanically. AI fixes it; the human just confirms the diff.

**Amber (human review mandatory):**
```
3. [review] stripe_adapter.py — interface consistency
4. [review] paypal_adapter.py — same (★ affects production PayPal env only)
5. [review] DB: payment_fees — consistency with fee rate master
```
Areas where overlooking causes incidents. Prefer not missing anything even if there is some noise.

**Gray (informational):**
```
6. [info] nightly_settlement.py — impact on nightly batch
7. [info] monthly_report.py — monthly report
```
Items that might be related. Review if time allows.

Thresholds vary by artifact type. For fee calculation code, anything even slightly suspicious is Amber. For a README, it takes something significant to reach Amber rather than Gray.

---

## Q&A: Common Questions About the Propagation Engine

### Q: Does it automatically fix everything, or does it just say "look here"?

**It depends on the band.**

| Band | AI's role | Human's role |
|------|----------|-------------|
| Green | **AI fixes it** | Just confirm the diff and approve |
| Amber | **AI presents multiple fix candidates** | Choose which one to adopt |
| Gray | **AI tells you the check points** | Read and judge yourself |

Example for the Amber band:
```
CoDD: "The signature of charge() has changed.
       paypal_adapter.py is also affected.

       Fix option A: Change to the same signature
       Fix option B: Absorb the change in the routing side of processor.py

       → Which would you like to adopt? [A / B / other manual approach]"
```

Not all human, not all automatic. The division of labor between AI and human varies by band.

"Fully automatic fixing" is the ultimate goal. At first, release it as a "safety net to prevent oversights." As trust accumulates, expand the Green band (the range of automatic updates).

### Q: The Green band is small at first, but does it grow? Why?

**Evidence accumulates as the team does ordinary work, and confidence rises.**

At the start (LLM inference only):
```
Convention edge: stripe_adapter → paypal_adapter:
  Evidence 1: LLM inference (score: 0.60)
  → Confidence: 0.60 → Amber (human review)
```

Three months later (evidence has accumulated):
```
Convention edge: stripe_adapter → paypal_adapter:
  Evidence 1: LLM inference (score: 0.60)
  Evidence 2: Co-changed in 11 of 12 commits in git history (score: 0.75)
  Evidence 3: Review comment "align the interface" (score: 0.90)
  Evidence 4: Changing only one caused an integration test to fail (score: 0.95)
  → Confidence: 0.9995 → Green (auto-update OK)
```

Committing, reviewing, failing tests, incidents — every one of these adds evidence to edges. Without any special effort, everyday work grows the graph.

Conversely, when the code undergoes a large refactoring, existing evidence becomes stale, confidence drops, and edges automatically revert from Green to Amber. There is a self-correcting mechanism.

### Q: Isn't it better to grow this as an organization rather than as individuals?

**Exactly. That is one of CoDD's core values.**

Today's reality:
```
Tanaka-san (20-year vet) in their head:  "If you change this, also look there"
Sato-san (15-year vet) in their head:   "That master affects that batch"
Suzuki-san (10-year vet) in their head: "This config is only active in production"
→ All of it scattered across individual heads. Disappears when they leave.
```

Put it in CoDD's dependency graph:
```
What Tanaka-san taught → convention edge (evidence: human:tanaka)
What Sato-san taught   → driven_by edge (evidence: human:sato)
What Suzuki-san taught → condition: "env == 'prod'" (evidence: human:suzuki)
→ Individual tacit knowledge is converted into a shared organizational asset (the graph).
→ Who contributed what is recorded. Others can refine it. It survives even if the person leaves.
```

Phase 3 (confirming with humans) and Phase 4 (learning from history) are organizational learning itself. With every review, every commit, and every incident response, the knowledge of the entire team accumulates in the graph.

| Problem | How CoDD Solves It |
|---------|-------------------|
| Knowledge siloed in individuals | Tacit knowledge is recorded in the graph. Survives even if the person leaves. |
| Knowledge discontinuity | New members can get up to speed by reading the graph. |
| Onboarding | "If you change this, here's what to check" comes from the system. |
| Variability in review quality | The graph supplements the review perspective. |

---

## Pillar 3: Context Management — What to Feed the AI

### The Problem

The impact list is produced. You want AI to suggest fixes for Amber-band items. But the quality of the fixes varies dramatically based on what you feed the AI.

```
Bad example 1: Feed in all source (100,000 lines)
  → Token count overflow. Cannot fit at all.
  → Even if it does fit, the middle sections are forgotten (Lost in the Middle problem).

Bad example 2: Feed in only the file being changed
  → "What is this method for?" is unknown.
  → The relationship with config values and master data is invisible.
  → Off-target fix suggestions come out.
```

### The Lost in the Middle Problem

A weakness of LLM (large language model) attention mechanisms.

```
Context window:
┌──────────────────────────────────────┐
│ [Top]    ← well remembered           │
│                                      │
│ [Middle] ← tends to be forgotten ⚠️  │
│                                      │
│ [Bottom] ← well remembered           │
└──────────────────────────────────────┘
```

What is at the top and bottom is well remembered, but information placed in the middle tends to be forgotten. If you put important code in the middle, AI will ignore it when producing fix suggestions.

### CoDD's Solution: The Dependency Graph Automatically Constructs the Context

Because the dependency graph exists, it is known "what is relevant to this change." That knowledge is used to automatically select the information to pass to the AI and arrange it in the optimal order.

```
Context window structure:

┌──────────────────────────────────────┐
│ [Top: well remembered]               │
│                                      │
│  1. The file being changed (full)    │
│  2. Intent and purpose of the change │
│  3. Directly related interfaces (full)│
│                                      │
├──────────────────────────────────────┤
│ [Middle: easily forgotten            │
│          → metadata only]            │
│                                      │
│  4. Indirectly related modules       │
│     (summary only)                   │
│     e.g., "This module does ○○"      │
│  5. Config value list with current   │
│     values                           │
│  6. Master data structure (DDL only) │
│                                      │
│  ※ No full code text.               │
│    Only information where forgetting │
│    is not catastrophic.              │
│                                      │
├──────────────────────────────────────┤
│ [Bottom: well remembered]            │
│                                      │
│  7. Summary table of impact set      │
│  8. Related past incident info       │
│  9. Checklist                        │
│     "Please verify the following"    │
│                                      │
└──────────────────────────────────────┘
```

Three key principles:

**Key 1: Target file at the top, checklist at the bottom.** Place important information where attention is high.

**Key 2: Do not put full code text in the middle.** Only metadata (a summary like "this module handles payment processing"). Forgetting it is not catastrophic.

**Key 3: Load the full text on demand.** If AI reviews the middle metadata and determines "I need to see this in full," it reads that file in full at that point. Do not put everything in up front.

### Decision Criteria for "What to Include"

The deciding factor is not importance but **ambiguity**.

```
Important but not ambiguous → summary is enough
  Example: "This module calls the Stripe API" (already obvious)

Important and ambiguous → load the full text
  Example: "The branching conditions of this module are unclear"

Not important → metadata only, or exclude
```

Many failures arise from thinking "it's important, so let's include the full text." The real criterion is "it's ambiguous, so the full text is needed." Even important things that are already obvious need only a summary.

### Integration with the Dependency Graph

```
1. The propagation engine produces the impact list
2. For each node, determine whether to include: full text / excerpt / summary / metadata
3. Select the most useful combination within the token budget
4. Arrange in a U-shape (important items at top and bottom)
5. Pass to AI
6. If AI determines "I need the full text here," load it additionally
```

Without the dependency graph, a human must decide what to include on every occasion. The dependency graph makes it possible to automatically determine "this change needs this information." This is the integration of Pillar 2 and Pillar 3.

---

## The Three Pillars Form a Loop

The three pillars are not independent features. They are all connected and cycle together.

### Overall Flow

```
【Pillar 1】 A requirement arrives
  │
  │  "Add multi-currency support to the payment feature"
  │
  ▼
  AI generates multiple "change proposals" against the dependency graph
  │
  │  Option A: Add currency column + modify adapters + add master data
  │  Option B: Add a new currency conversion layer + leave existing code untouched
  │
  ▼
  Human chooses → adopts Option A
  │
  │  → At this point, "what changes" is determined on the graph
  │
  ▼
【Pillar 2】 Propagate impact via dependency graph
  │
  │  By selecting Option A:
  │  - Add currency column to payment_fees (SchemaChanged)
  │  - Change processor.py arguments (InterfaceContractChanged)
  │  - Both stripe and paypal adapters are affected (via switched_by)
  │  - Section 3 of the design document is affected (via implements)
  │  - Multi-currency tests need to be added to the test spec (via validated_by)
  │  - Nightly batch may be affected (via co_changed_with)
  │
  │  → Classified as Green / Amber / Gray
  │
  ▼
【Pillar 3】 Pass optimal context to AI
  │
  │  For the Amber-band stripe_adapter fix:
  │  - Top: stripe_adapter.py full text + processor.py interface
  │  - Middle: paypal_adapter summary + payment_fees DDL
  │  - Bottom: Checklist — "unify handling of currency"
  │
  ▼
  AI produces fix suggestions
  │
  │  "How about fixing stripe_adapter.py like this? (Option A / Option B)"
  │
  ▼
  Human reviews → adopts → commits
  │
  ▼
【Back to Pillar 2】 Commit auto-updates the dependency graph
  │
  │  Phase 1 runs → new edges added / updated
  │  One more piece of evidence → confidence rises slightly
  │
  ▼
  The next change propagates with even higher accuracy
```

### Loop Diagram

```
Pillar 1 (Design generation) ──→ Pillar 2 (Impact propagation) ──→ Pillar 3 (Context optimization)
      ↑                                                                     │
      │                                                                     │
      └──────────────────── Feedback ──────────────────────────────────────┘
                      (Fix results grow the graph)
```

- Pillar 1 decides "what to change"
- Pillar 2 produces "what is affected"
- Pillar 3 decides "what to feed the AI"
- AI produces fix suggestions → human confirms → commit
- Commit grows Pillar 2's graph
- Accuracy improves in the next cycle

**The more it is used, the smarter it becomes. The three pillars are not separate features but a single cycling system.**

---

## The Essence of CoDD: The Crystallization of Organizational Knowledge and Experience

Technically it is a "change impact analysis tool." But its essence is different.

```
Dependency graph = source code structure (the part machines can read)
                 + framework configuration info (the part machines can query)
                 + runtime behavior (the part machines can observe)
                 + experienced engineers' knowledge (the part extracted from humans)
                 + team history (the part learned from git / incidents / reviews)
```

In other words, **"the knowledge and experience of everyone who has ever worked on the system" crystallizes in the graph.**

### Why Traditional Knowledge Management Fails

| Existing approach | Problem |
|------------------|---------|
| Documentation (Confluence / Notion) | Nobody writes it. Even if written, it is never updated. It rots. |
| Handover documents | Written in a panic just before someone leaves. Full of gaps. |
| Pair programming / mob programming | Knowledge transfers but is not recorded. |
| Code comments | Nobody writes them. Even if written, they explain "what" not "why." |

### Why CoDD's Dependency Graph Is Different

CoDD's dependency graph is not "deliberately written" — it **grows as a byproduct of everyday work**.

- CI auto-updates on every commit (does not rot)
- Runtime observes every day (grows automatically)
- Human knowledge is added on every review (grows through use)
- When an incident occurs, evidence is automatically added (painful lessons become knowledge)
- "Why" is preserved — Evidence records "previous incident" and "review comment"
- Survives after people leave — crystallized in the graph

### CoDD in One Line

```
Technical description:
  "Change impact analysis using a conditional evidence graph"

The description that resonates with people:
  "The knowledge and experience of the entire team crystallizes
   in a dependency graph that grows the more you use it.
   People leave, but knowledge remains."
```

---

## Where CoDD Is Heading: AI Auto-Fix and Semi-Automated Enhancement

As the dependency graph matures, the range of AI automation expands.

### Level 1: Notify of Impact (Initial Stage)
```
Human: "I'm changing this"
CoDD:  "This is also affected" (Green / Amber / Gray)
Human: Fix manually, or choose from AI's fix suggestions
```

### Level 2: Handle Fixes Almost Automatically (Expanding the Green Band)
```
Human: "I'm changing this"
CoDD:  Identify impact + auto-fix Green band + present fix suggestions for Amber band
Human: Only make decisions for the Amber band
```

### Level 3: Auto-Fix Incidents
```
Monitoring: "Error in production"
CoDD:        Error location → identify root cause candidates via dependency graph
             → "Commit X is 95% likely to be the cause"
             → Auto-generate fix patch → auto-run tests → pass
Human:       Just review the patch and merge
```
Because the dependency graph records "this commit changed this edge," traversing backward from the error location leads to recently changed edges.

### Level 4: Semi-Automate Enhancements
```
PM:   "Add a subscription feature"
CoDD:  Analyze requirements → generate multiple change proposals → compare impact scope
       → Recommend the lowest-risk option
       → Generate code, tests, design documents, and migrations
Human: Design decisions + final review only
```

### Why CoDD Makes This Possible

Differences from ordinary AI tools (Cursor, Copilot, etc.):

| | Ordinary AI | AI with CoDD |
|--|------------|--------------|
| Context | Currently open files + surroundings | Dynamically constructed from dependency graph |
| System-wide impact | Unknown | Known |
| Config-driven dependencies | Invisible | Visible |
| Past incident patterns | Unknown | Known |
| Veteran knowledge | Absent | Crystallized in the graph |

The AI's capability is the same. The quality of the context provided differs by orders of magnitude.

### Conditions for Reaching Each Level

Levels 1–2 are achievable quickly. Conditions needed for Levels 3–4:
- A sufficiently mature dependency graph (Month 3+)
- High test coverage
- A well-established CI/CD pipeline
- Green band precision of 95% or higher

Simply doing Levels 1–2 automatically progresses toward Levels 3–4. Everyday work grows the graph, accuracy improves, and the range of automation expands.

---

## CoDD's Limitations (Honestly)

### What Cannot Be Solved in Principle

1. **Unrecorded intent cannot be reconstructed.** Business intent that exists nowhere in code, documentation, or traces is inherently weak.
2. **Rare paths are hard to observe.** A batch that runs only once a year does not accumulate observation data.
3. **History also learns bad habits.** Co-change patterns that included bugs in the past are also remembered.
4. **100% is impossible.** The possibility of "unknown dependencies" always remains.

### How Much of a Problem Is This in Practice? (From Q&A)

> **Q: You say limitation 1 is "intent disappears," but wouldn't static code analysis still reveal the structure so dependencies can be traced?**

You're right. Even if "the reason for why it's done that way" disappears, the structural dependency "changing this affects that" is visible through AST analysis. That is sufficient for impact analysis. Limitation 1 is "the intent cannot be explained," not "the dependency is invisible."

> **Q: You say rare paths can't be observed, but don't we typically never touch rare paths anyway?**

You're right. A batch that runs once a year is almost never the target of routine changes. The exception is modernization (full system replacement). In that case, intentionally run that path once to capture a trace.

> **Q: Are these limitations ultimately fatal?**

They are not fatal. None of them lead to "therefore CoDD is unusable." This section has a strong flavor of **academic convention**. In academic papers, claiming "our method is universal" results in rejection at peer review, so weaknesses are stated proactively. Writing "this is principally beyond our scope, but it is unlikely to be a problem in practice" actually increases credibility.

### A Remedy for Limitation 3: "Learning Bad Habits"

> **Q: If history data learns incorrect dependencies, is there a countermeasure?**

There are three remedies.

**Remedy 1: Cross-reference with bug-fix commits.** If a co-change of A and B is frequently followed by a revert of B or a bug-fix commit, judge "the A→B co-change was likely a bug" and lower the confidence.

**Remedy 2: Evidence Ledger majority vote.** Even if history-based evidence says "A→B," if static analysis, framework analysis, and execution traces all fail to corroborate A→B, the Noisy-OR calculation still yields low confidence. Dependencies claimed only by history are automatically classified as Amber (needs review).

**Remedy 3: Human feedback overrides.** If a human marks "this dependency is wrong" in Phase 3, it is recorded as negative evidence in the Evidence Ledger. When the same pattern appears again, the confidence is lower.

It cannot be entirely prevented, but "bad learning running amok" can be prevented. **The design ensures that history-only evidence alone can never place an edge in the Green band.**

### That Is Why CoDD Does Not Aim for Perfection

The goal is "70% coverage of a veteran's knowledge." The remaining 30% is covered by:
- Tests (if it breaks after a change, you notice)
- Monitoring (if there is an anomaly in production, you notice)
- Human review (the last line of defense)

CoDD is a **safety net**, not a silver bullet.

---

## CoDD Is "Infrastructure for AI to Do Its Best Work," Not "AI-Centric"

> **Q: Isn't CoDD a completely AI-centric development methodology?**

It looks that way at first glance, but it is not.

Pillar 1 (dependency graph) and Pillar 2 (propagation engine) are **completely deterministic**. AST analysis, static analysis, framework analysis, SQL parsers. No AI is used. This is the foundation.

CoDD's structure:
- **Foundation**: Deterministic dependency graph + propagation algorithm (zero AI)
- **Middle layer**: AI supplements "the missing parts" (LLM classification in Phase 2, inference for Layers 3–5)
- **Output layer**: AI "uses the results to do work" (context optimization, auto-fix)

AI is not at the center. Rather, **CoDD is infrastructure that prepares data so AI can do its best work**.

### Differences from Existing AI Development Tools

Today's Claude Code and Copilot read the codebase from scratch and infer dependencies every time. They are like "a veteran SE who wakes up with amnesia every morning."

| Item | Without CoDD | With CoDD |
|------|-------------|-----------|
| Dependency awareness | AI infers each time | References a pre-computed graph |
| Scope of impact | AI says "probably here too" | "Here, reason is this" with evidence |
| Context | Dump everything or rely on intuition | Only what is needed, optimally placed |
| Fix suggestions | Proposed based only on the code fragment | Proposed considering upstream to downstream |

**Conclusion**: CoDD is not "an AI-centric development methodology" but **"infrastructure that makes traditional development AI-ready."** Rather than placing AI at the center, it structures the system's knowledge so that AI can exert maximum capability. The protagonist is "structured dependency knowledge"; AI is a consumer of that knowledge.

Existing AI development tools (Copilot, Cursor, Claude Code) aim to "make AI smarter." CoDD aims to "make the data fed to AI smarter." That is the decisive difference.

### Reversal of Roles: Toward an Era Where Humans Support AI

> **Q: Doesn't this reverse to "humans supporting AI" rather than "AI supporting humans"?**

Exactly. Traditional AI-assisted development is "humans design → humans code → AI supplements." The human is the protagonist; AI is the assistant.

When CoDD evolves to Levels 3–4, it becomes "AI identifies impact scope → AI generates fix suggestions → humans approve." **AI is the protagonist, and humans become reviewers who "confirm it's correct."**

This is a paradigm shift. The reason GPT-5.4 evaluated CoDD as "suited for a vision paper" is precisely because it addresses exactly this kind of large directional shift.

### The Perfect Foundation for an "AI-Driven Organization"

The trend of 2025–2026 is "AI-driven organizations" and "AI-native companies" — ways of working where AI is the protagonist and humans are the supplement. But no one has answered "how to concretely realize this in software development."

Today's AI development tools (Copilot, Cursor, Claude Code, Devin) all move in the "human → AI" direction. There is no knowledge base enabling AI to make autonomous development decisions. That is why humans must explain dependencies every time.

CoDD answers this. It provides a structured knowledge base for AI to make autonomous development decisions.

- Smarter AI → wait for model evolution (outside CoDD's scope)
- Better prompts → remains human-dependent (does not scale)
- **Structured knowledge that AI can reference** → what CoDD provides

**CoDD is a concrete methodology for realizing "AI-driven development."** The value lies not in technological novelty but in "posing the question itself in a new way."

The core of the vision paper: "While current AI-assisted development tools augment human developers, the emerging paradigm of AI-driven organizations demands the inverse: structured knowledge infrastructure that enables AI to lead development decisions, with humans providing oversight."

### Generalizability Beyond Software Development

> **Q: Can CoDD be applied not just to development but to organizational activities in general?**

It can. Abstracting the problem CoDD solves: "in a complex system, when one element changes, which other elements are affected?" This is not limited to code.

The same problem occurs in organizations:
- **Personnel transfers**: Removing person A eliminates an implicit point of contact for Project B → handover is missed
- **Internal policy change**: Changing the expense policy requires also changing the approval workflow and accounting system settings
- **Legal amendment response**: Invoice system reform → invoices, accounting entries, vendor master, contract templates — all affected
- **Organizational restructuring**: Department merger → permission tables, mailing lists, approval routes, seating charts — all change

CoDD's three pillars can be mapped directly:
- Code dependency graph → dependency graph of business processes, policies, people, and systems
- Conditional edges → "only this department" or "only this contract type"
- Evidence Ledger → evidence base (policies, contracts, meeting minutes)
- Propagation engine → automatic notification of "if you change this, change here too"
- Context optimization → deliver only changes relevant to the responsible party

However, as a publication strategy, the correct approach is to first focus on software development. Claiming "it works for everything" results in reviewers saying "you've proven nothing." Build a track record in cs.SE, then generalize.

---

## Two Layers: Propagation Within Code and Propagation Across Development Phases

CoDD's dependency graph has two layers.

### Layer 1: Dependency Propagation Within Code
Dependencies inside the system, such as `Service.java → Repository.java → tests`. Handled by Pillar 2's propagation engine.

### Layer 2: Propagation Across Development Phase Artifacts
Dependencies among V-model artifacts, such as `Requirements → High-level Design → Detailed Design → Code → Test Specification → Operations Guide`. Handled by Pillar 1's "bidirectional sync between design documents and code."

### Both Layers Are in a Single Graph

The CEG node types include both artifacts and code:
- requirement (items in requirements definition)
- design (sections in high-level and detailed design)
- code (classes, methods, configuration files)
- test (test cases, items in test specification documents)
- operation (operations guides, monitoring configurations)
- config (application.yml, environment variables)
- data (table definitions, master data)

Both Layer 1 (within code) and Layer 2 (across phases) are handled by **the same graph and the same propagation engine**.

### Bidirectionality Is CoDD's True Strength

Propagation goes not only top-down but also bottom-up. Adding a column to `point_master` → add column definition to detailed design → update the ER diagram in high-level design → confirm in requirements. What used to be traced by humans entirely in their heads is now automatically surfaced as "also check here" by traversing the graph.

---

## Summary

```
CoDD = Conditional evidence graph (dependency management)
     + Change propagation engine (automatic impact identification)
     + Context optimization (information selection for AI)
```

**What it provides:**
- "What might be related" (impact list)
- "How far you need to look to be safe" (Green / Amber / Gray)
- "Why this is claimed" (evidence + explanation of impact path)
- "Accumulation of organizational knowledge" (a knowledge base that survives after people leave)

This is a practically usable "weak mental model."
