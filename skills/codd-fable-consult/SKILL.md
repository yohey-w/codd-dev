---
name: codd-fable-consult
description: |
  Consult Claude Fable 5 (Anthropic's most capable model for deep, long-horizon reasoning — or whichever model currently holds that role) for a genuinely hard, novel CoDD architectural design decision, where a quick mechanical patch would be wrong. Packages CoDD's philosophy, invariants, GitHub repo, and session-derived insights into a rigorous consultation prompt; reviews the returned design before any implementation begins. Use AFTER a bug/gap has already been precisely diagnosed — this is a design skill, not a diagnostic one.
---

# CoDD × Fable — Strategic Design Consultation

Route a genuinely hard CoDD design fork to Claude Fable 5 instead of guessing, drive-by patching, or letting a subagent design in a vacuum. This skill exists because a real consultation this project ran (the Java/C#/C++ marker-authenticity delegated-assertion gap, 2026-07-01/02) produced a design far better than an ad-hoc prompt would have — but only once several specific ingredients were deliberately injected. Skip any of them and you get generic, ungrounded, or overfit advice instead.

## When to Use

Only after diagnosis is already done and the case genuinely meets **all** of these:

- A real bug/gap has been root-caused with concrete evidence (ideally from a live dogfood run, not a hypothetical) — not "something might be wrong," but "here is exactly what fails and why."
- A quick mechanical patch would be **wrong**, not just tedious. Signs: the fix would hardcode a framework/instance-specific special case into shared code; the fix could be done three different reasonable ways with real tradeoffs; the fix spans multiple language adapters and a per-language patch would duplicate audited logic; there's a live risk of solving today's instance while creating tomorrow's overfit.
- The decision is squarely inside CoDD's own stated hard invariants (generality-first, anti-false-green/anti-false-red, owner-not-a-bottleneck) such that getting it wrong would violate one of them, not just be suboptimal.

**Do NOT use this for:**
- A bug with one obviously correct fix — just fix it, and cite `codd/coverage_execution_coherence.py`-style commits (this project has many) as the bar for a well-scoped, well-tested mechanical patch.
- Exploratory "what should we build next" prioritization — that's a Gunshi/owner conversation, not a design-fork consultation.
- Anything where the honest answer is "we don't have enough live evidence yet" — go get a live failure first (via a normal investigation agent), then come back here.

## Why the Naive Version Fails (what this skill fixes)

A first attempt at this consultation used the Agent tool's subagent routing with `model: "fable"` and failed instantly ("Claude Fable 5 is currently unavailable") — that routing path was not reliable even when Fable 5 access itself was confirmed working via direct CLI. **Always invoke Fable 5 via direct CLI, not the Agent tool's model parameter**, until/unless that routing is independently reconfirmed (see Invocation below).

A second, more subtle failure mode: even a well-intentioned prompt that just describes the problem in prose gets you a plausible-sounding but *ungrounded* or *genericized* answer. The prompt structure below exists because each ingredient closed a specific gap observed in practice:

| Missing ingredient | What goes wrong without it |
|---|---|
| The actual GitHub URL | Fable 5's own sandboxed session may have filesystem access denied by its execution environment's own permission profile (observed directly: Read/`ls`/`grep`/`git fetch` all auto-denied in one real run) — with no repo URL, it either refuses or speculates about code it never saw. Handing it `https://github.com/yohey-w/codd-dev` up front let it self-recover by reading the real, current `origin/main` via web-accessible tools instead. |
| CoDD's philosophy stated explicitly, not implied | Without explicit "generality-first: no `language ==` in shared code" and "anti-false-green: nothing may let an AI certify its own homework," a generically-smart model will still reach for the locally-obvious fix (a per-language special case, or weakening a gate) because that reads as "pragmatic" absent the stated constraint. |
| "The design must stay generic" as an *explicit, named* requirement | Otherwise you get a correct-looking fix for the ONE framework/instance you showed it (e.g., "special-case ArchUnit's `.check()`") without the model ever being asked to defend that choice against the next framework that shows up. |
| A concrete adversarial-case demand | Without asking for adversarial examples the design must still reject, you get a design that solves the reported case but has no evidence it preserves the anti-false-green invariant against a lightly-different attack. |
| "End with ONE recommended path" | Absent this, capable models default to an evenhanded survey of options — useful for exploration, useless as an implementation ticket. |
| A staged/live-verification rollout ask | Without this, the returned plan tends to be "ship everything at once" — this project's own discipline (a live dogfood rerun per increment, not just `pytest` green) needs to be requested, not assumed. |

## Invocation

**Direct CLI, not the Agent tool.** Write the prompt to a scratchpad file first — a multi-paragraph prompt inlined into a shell command is an escaping nightmare — then invoke via `nohup ... & disown` so the consultation survives independently of whatever is currently killing long-running `run_in_background: true` Bash tasks in this environment (observed, unresolved root cause as of 2026-07-02 — resuming/re-launching reliably recovers regardless of cause, and a fully-detached process sidesteps the whole question).

```bash
# 1. Write the prompt (see template below) to a file.
#    Use the Write tool for this, not a heredoc in the same command.

# 2. Launch detached, capturing stdout/stderr separately:
nohup claude --print --model claude-fable-5 --effort max "$(cat /path/to/prompt.md)" \
  > /path/to/design_output.md \
  2> /path/to/design_output.stderr &
disown
echo "launched pid=$!"

# 3. There is no automatic completion notification for a nohup'd process —
#    you must poll. Check both that the process has actually exited AND that
#    the output file has real content before treating it as done:
ps aux | grep "claude.*fable-5" | grep -v grep   # empty = finished (or never started — check stderr too)
wc -l /path/to/design_output.md
```

If a quick sanity check is warranted first (confirming Fable 5 access itself, independent of the design question), a trivial low-cost probe works and returns fast: `claude --print --model claude-fable-5 --effort low "reply with exactly the word: available"`.

## Prompt Template

Fill in the bracketed sections. Keep every other section close to verbatim — they are the ingredients from the table above, not filler.

```
You are being consulted specifically because this is a genuinely hard, novel
architectural design problem in CoDD (a context-driven/coherence-driven
development tool) at [REPO: https://github.com/<owner>/codd-dev — grounded
reasoning only; if you cannot reach the local filesystem, read the actual
current code from this GitHub repo (origin/main) via whatever web-access
tools you have, and disclose which access path you actually used]. This is
NOT a "find and fix a bug" task — diagnosis is already done. This is a
"design the right generalizable solution" task.

## CoDD's non-negotiable invariants (violating any of these defeats the point of asking you)

- **Generality-first**: the language-free CORE never branches on
  `language == "python"` etc. Per-language behavior lives entirely in
  declarative YAML profiles (`codd/languages/profiles/*.yaml`) plus small,
  isolated adapter classes registered per language. Any fix you design MUST
  fit this shape — verify by grepping the actual code, don't assume.
- **Anti-false-green**: when an AI autonomously writes both source code and
  its own tests, nothing may let it certify its own homework. Gates exist to
  independently verify claims, never to trust self-report.
- **Anti-false-red**: a gate must not fail something that is actually fine —
  a standing false-red trains operators toward disabling the gate entirely,
  which is a worse outcome than the false-red itself.
- **Owner-is-not-a-bottleneck**: prefer designs where extending coverage
  (a new language, a new library, a new edge case) is a data/config edit
  reachable by anyone, not a core-code change requiring the maintainer.

## The specific gap (concrete, evidence-based — not hypothetical)

[DESCRIBE: the exact failure, with real file paths, function names, and
live evidence — what broke, on what real input, with what exact error
text. Cite the specific files/functions you've already read so Fable 5
grounds itself immediately rather than re-discovering them.]

[STATE: why a quick patch is wrong — what got tried or considered and
rejected already, and specifically what overfit/generality risk a naive
fix would create. If a prior investigation flagged this as a design
fork rather than a bug, quote that flag.]

## What I need from you

A concrete, opinionated, implementable design — not a survey of options with
no conclusion.

1. [The core mechanism question — e.g., "should this be N per-language
   implementations or a shared primitive with per-language plug-ins, and
   why, grounded in what's already in the code?"]
2. [Any depth/bound/stopping-rule question the design needs to answer
   honestly, not just practically.]
3. [The hardest sub-problem — usually "how do we handle the
   framework/instance-specific case generically without hardcoding it,"
   with the overfit risk stated explicitly and NOT to be dodged.]
4. **Anti-false-green integrity**: for anything you propose, give at least
   one adversarial example your design correctly REJECTS, alongside the
   real motivating example(s) it correctly ACCEPTS.
5. **Scope call**: should this ship as one unit, or is there a safe, smaller
   first increment that de-risks the rollout, gated by a LIVE rerun of the
   actual failing case (not just unit tests) before the next increment?
   Weigh in using CoDD's own stated invariants explicitly.

## Constraints

- Do not write or edit any code yet — sketch signatures/pseudocode if
  confident, but this is a design pass.
- Do not propose disabling or weakening the affected gate/check — that was
  already considered and rejected.
- Ground everything in the actual code. Read the real files; don't assume.

## Output

A design document, written directly in your response. Structure it however
best serves clarity, but it MUST end with a single clear recommended path
(not a menu with no pick) and an explicit, ordered implementation task list
for a follow-up task to execute against.
```

## After the Consultation Returns

1. **Read the disclosure, if any, before the design.** Fable 5 (or any consultant) may state what it could and couldn't access, and what it grounded itself in. Treat an honest "I couldn't read your local files so I read GitHub instead" as a *positive* signal (it's telling you exactly how to weight its claims), not a defect. Treat suspiciously specific claims with NO stated grounding (exact internal variable names, oddly-precise numbers, citation-free specifics) as a reason for skepticism — cross-check anything load-bearing before trusting it, the way you would any other unverified research output.
2. **Verify the design actually reads the real, current code**, especially if local uncommitted changes exist that the consultation couldn't see. A design grounded in a stale or public-only view of the repo may miss same-day local edits.
3. **Do not implement blind.** Skim the design for: does it end in one recommended path (not a menu)? Does it give adversarial cases, not just happy-path cases? Does it avoid hardcoding an instance-specific special case into shared code? If any of these are missing, the consultation likely needs a follow-up round, not a rubber-stamp.
4. **Route the follow-up implementation through the project's normal discipline** — full test suite, a regression test per adversarial case the design specified, no `language ==` branches, staged/incremental landing gated by a live rerun per the design's own rollout plan, targeted `git add` (never `-A`).
5. **Report back to the user with the TL;DR and the single recommended path** before starting implementation, even when the user has pre-authorized "implement what Fable 5 says" — a one-paragraph confirmation costs little and catches the case where the design, on reflection, doesn't actually fit.

## Absolute Constraints

1. **Never skip the invariant-and-repo-URL preamble to save prompt length.** Every ingredient in the table above was added because its absence produced a measurably worse result. This is not boilerplate to trim.
2. **Never accept a design that special-cases a specific framework/library/instance by name inside shared/dispatch code.** If the hard sub-problem in your consultation is exactly this shape, the returned design must resolve it via declarative data (profile YAML) or an explicitly-argued generic mechanism — not a Python `if name == "ArchUnit"` (or equivalent) in core logic.
3. **Never implement a returned design without at least skimming it for the four checks in step 3 of "After the Consultation Returns."**
4. **Never treat Fable 5's output as ground truth about its own execution environment or about anything it couldn't ground in actual code** — its disclosed access limitations are real constraints on confidence, not modesty.

## Troubleshooting

- **"Claude Fable 5 is currently unavailable" via the Agent tool** — this is a known routing gap (subagent `model: "fable"` failed even when direct CLI access was confirmed working, 2026-07-02). Fall back to direct CLI invocation per the Invocation section; do not conclude Fable 5 itself is down without an independent direct-CLI probe.
- **The consultation process seems to vanish with no notification** — expected for a `nohup`'d process; there is no task-notification. Poll the output file and `ps` directly.
- **The design references code that doesn't match your local tree** — check whether local uncommitted changes exist that predate/postdate what the consultation could see (its disclosure should say what commit/branch it grounded on); re-verify the relevant sections before implementing rather than assuming staleness invalidates the whole design.

## Why This Skill Exists

CoDD's hardest design questions are exactly the ones a fast, mechanical patch gets wrong — they're rare, but when they show up (a cross-language architectural gap, a genuine generality-vs-overfit tradeoff, a gate-design tradeoff touching the anti-false-green invariant), the cost of a bad call compounds across every future language/framework CoDD onboards. A single, well-grounded consultation with the right context injected is cheap insurance against that; an ungrounded or under-specified one is worse than not asking, because it produces false confidence. This skill is the checklist that keeps the consultation grounded, generic-by-construction, and honest about its own evidence — learned from the first real run of this pattern, not designed in the abstract.
