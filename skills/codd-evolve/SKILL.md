---
name: codd-evolve
description: |
  Conversationally evolve an existing CoDD project. Use when the user describes a functional change in natural language ("add logout button", "change course model to master + delivery target", "remove daily log step") and you need to update requirements, design docs, lexicon, source code, and tests together while maintaining CoDD coherence. Brownfield modification, NOT greenfield generation, NOT pure bug fix.
---

# CoDD Evolve — Conversational Brownfield Evolution

Take a Lord-style natural-language change request and automatically determine which design docs, lexicon entries, source files, and tests must move together to preserve CoDD coherence. The user expresses intent; this skill figures out scope.

## When to Use

- The user describes a **functional change** to an existing system, not a bug
- Examples of trigger phrases:
  - "Add a logout button to the admin nav"
  - "Change course management — courses should be a shared master with separate delivery targets"
  - "Restructure the learner list to drill down: facility → learner → course → progress"
  - "Remove the daily log append step from the karo completion flow"
  - "受講者管理を施設フィルター起点に変更"
- The user does NOT want to think about *which* design docs, lexicon entries, or source files to touch
- The project is already CoDD-initialized (has `codd/codd.yaml` and at least the design doc layout)

Do NOT use this for:

- Greenfield generation from scratch — use `/codd-init` then `/codd-generate`
- Pure bug fix where requirements and design are correct — use `codd fix` or `codd fix [PHENOMENON]`
- Reverse-engineering an undocumented codebase — use `codd extract` (or `/codd-restore`)
- Single-doc impact analysis only — use `/codd-impact`
- Code-only refactoring with no behavioral change — use `codd propagate`

## What This Does — Role Separation

This skill makes a single contract explicit:

| Layer | Who decides | What |
|-------|-------------|------|
| Intent | **User** | "I want X" in natural language |
| Strategic constraints | **User** | North star, hard prohibitions, breaking-change tolerance |
| Impact scoping | **This skill** | Which design docs, lexicon, source files, tests are affected |
| Doc updates | **This skill + CoDD** | Update requirements + every affected design doc in coherent order |
| Lexicon updates | **This skill** | Detect new terms, ask user before adding, then update lexicon |
| Implementation | **CoDD CLI** | `codd implement` from updated design |
| Verification | **CoDD CLI** | `codd verify` — must reach red 0 |
| Coherence finalization | **CoDD CLI** | `codd propagate` for cross-doc consistency |
| Failure judgment | **You (orchestrator)** | Decide retry vs ask user vs abort |
| Final approval | **User** | Review the PR / diff post-hoc |

The user must never have to choose *which file* to touch.

## Workflow

### Step 1 — Confirm prerequisites

Before starting, verify:

1. Current directory is a CoDD-initialized project (`codd/codd.yaml` exists)
2. Working tree is clean OR uncommitted changes are intentional (warn the user otherwise)
3. `codd verify` currently passes (red 0) — if not, the user should fix existing red first, per `codd fix [PHENOMENON]` prerequisite

If red exists, STOP and surface it. Do not attempt to layer new changes on a red baseline.

### Step 2 — Parse intent and classify

Classify the user's request into one of:

| Type | Marker | Likely affected docs |
|------|--------|---------------------|
| `add_feature` | "add X", "新規追加" | requirements + at least one design doc + lexicon (new terms) + new source + new tests |
| `change_behavior` | "change X to Y", "〜に変更" | requirements + affected design docs + lexicon (term-meaning shift) + modified source + updated tests |
| `change_data_model` | "data model", "schema", "table", "entity" | database_design + api_design + lexicon + migrations + source + tests |
| `change_ux` | "UI", "screen", "navigation", "画面" | ux_design + frontend source + frontend tests |
| `remove_feature` | "remove X", "削除" | requirements (mark removed) + design docs (remove sections) + source (remove) + tests (remove or update) + lexicon (deprecate term) |
| `cross_cutting` | Touches auth/permissions/i18n/tenancy | auth_design + every callsite + tests for every role |

If classification is ambiguous, ask one clarifying question (see Step 3).

### Step 3 — Stop-and-ask gates

Stop and ask the user **only** when one of these triggers fires:

1. **New lexicon term required.** The change introduces a vocabulary not in `project_lexicon.yaml`. Ask: "I'll add `<term>` to the lexicon meaning `<definition>`. OK?"
2. **Breaking change to existing behavior.** Existing users / callers will see different output. Ask: "This changes how `<X>` behaves for existing users. Is breaking change acceptable?"
3. **Coherence is structurally impossible.** Requirements would contradict an existing invariant. Surface the contradiction; do not proceed silently.
4. **Cross-cutting scope explosion.** The change touches more design docs than the user likely realized (rule of thumb: >4 docs). Confirm scope before charging ahead.
5. **Ambiguous role/scope.** "Add logout" — for which role? Or all roles? Ask once.

**Do not** ask the user:
- Which file to touch
- Which doc to update
- What order to do things in
- What to name a commit
- Which version to bump

### Step 4 — Execute the coherence chain

Once intent is confirmed, execute in this order (each step's output feeds the next):

```
1. Update requirements/*.md
   - Append new requirement / modify existing / mark deprecated
   - Preserve frontmatter, traceability IDs, and Bloom levels

2. Update affected design/*.md docs (in dependency order)
   - Determine order via codd's existing CEG (depends_on graph)
   - Update each doc body to reflect the new requirement
   - Preserve frontmatter exactly

3. Update project_lexicon.yaml if needed
   - Only after user confirmed in Step 3
   - Keep alphabetical / grouped order if existing convention

4. Run codd implement to (re)generate source from updated design
   - For incremental change, codd implement updates only affected modules
   - For pure data model changes, also generate migration files if applicable

5. Update tests
   - Tests for new requirements MUST be added (no silent skip)
   - Tests for removed requirements MUST be removed
   - Tests for changed behavior MUST be updated

6. Run codd verify
   - MUST reach red 0
   - If red persists, see Step 5 (failure handling)

7. Run codd propagate
   - Final cross-doc consistency pass
   - Catches any drift between source-as-implemented and design-as-written
```

Never reorder these steps. Doc updates always precede source updates — that is the CoDD coherence invariant.

### Step 5 — Failure handling

If `codd verify` red persists after Step 4:

1. First retry: run `codd fix` once to let CoDD self-repair common issues
2. Second attempt: surface the failing test output, classify the cause
   - Test outdated → update test
   - Design contradicts requirement → ask user
   - Implementation cannot match design → ask user whether design is wrong or impl approach is wrong
3. Do not loop more than 3 times. After 3 failed attempts, STOP and report to user with concrete diagnostics

### Step 6 — Report

Generate a concise summary for the user:

```
Updated:
- requirements/foo.md (added: ログアウト機能)
- design/auth_design.md (added: NextAuth signOut handler)
- design/ux_design.md (added: 中央管理者ナビ Logout ボタン)
- src/components/AdminNav.tsx (added)
- src/app/api/auth/signout/route.ts (new)
- tests/e2e/logout.spec.ts (new)

Lexicon: no changes
Verify: red 0 ✅
Propagate: 0 drift ✅
```

Suggest a commit message and offer to commit. Do not auto-commit unless the user confirms.

## CoDD Commands This Skill Orchestrates

| Command | When invoked | Why |
|---------|-------------|-----|
| `codd verify` (entry guard) | Step 1 | Confirm clean baseline |
| `codd impact` | Step 2 | Determine which design docs are downstream of the proposed change |
| `codd implement` | Step 4 (step 4) | Generate source from updated design |
| `codd verify` | Step 4 (step 6) | Confirm coherence after change |
| `codd fix` | Step 5 (retry) | Self-heal common verify failures |
| `codd propagate` | Step 4 (step 7) | Catch final source-design drift |

## Examples

### Example 1 — Feature addition (clear scope)

User: "Add a logout button to the admin nav."

Skill:
1. Classify → `add_feature`, scope = auth + ux (single role: admin)
2. No new lexicon, no breaking change → no Step 3 questions
3. Execute chain → requirements + auth_design + ux_design + impl + tests + verify + propagate
4. Report → "Added logout for central_admin role. 6 files touched, verify ✅"

### Example 2 — Data model change (asks once)

User: "Course management should be a shared master with separate delivery targets."

Skill:
1. Classify → `change_data_model`, scope = database + api + lexicon + migrations + ux
2. Step 3 trigger: new term `delivery_target` (`配信先`) not in lexicon → ASK
   - "I'll add `delivery_target` to the lexicon as 'a tenant/facility to which a course is distributed; many-to-one with course'. OK? Also, this changes the existing 1-course-1-tenant structure — migration required. Breaking change for existing course records is acceptable?"
3. After user confirms → execute chain including migration generation
4. Report

### Example 3 — Reject as bug-fix territory

User: "The login page sometimes times out on slow networks."

Skill:
- This is a bug, not a functional change. Decline politely:
  - "This sounds like a bug. Use `codd fix \"login times out on slow networks\"` instead — `codd-evolve` is for intentional design changes."

### Example 4 — Reject as greenfield

User: "Start a new SaaS project for restaurant reservations."

Skill:
- This is greenfield. Decline:
  - "For new projects, use `codd init` then `codd plan` followed by `codd generate`. `codd-evolve` is for evolving existing CoDD projects."

## Absolute Constraints

These are non-negotiable. Violating any of them defeats the purpose of CoDD:

1. **Never edit source without a corresponding design doc update.** If the change requires source modification, requirements and design must already reflect it.
2. **Never silently introduce a new lexicon term.** Always ask the user first.
3. **Never proceed past a red `codd verify`.** Either retry (max 3) or stop and ask.
4. **Never reorder the chain.** Requirements → design → lexicon → source → tests → verify → propagate. No shortcuts.
5. **Never bypass user approval for breaking changes.** "Breaking" means: existing API contract changes, existing data semantics change, existing user-visible behavior changes.
6. **Never skip tests for new requirements.** A new functional requirement without a corresponding new test is incoherent.
7. **Never commit without user approval.** Stage and propose, but do not commit autonomously.

## Guardrails

- Use the `codd` command, not `python -m codd.cli`
- Run from the project root (where `codd/codd.yaml` lives)
- Each invocation should handle one logical change. If the user bundles multiple unrelated changes ("add logout and also restructure the course list"), split into separate runs
- Preserve all frontmatter exactly — only modify doc bodies and append/remove sections as needed
- When updating docs, do not gratuitously reformat unchanged sections — minimal diff is a feature
- If the user is on a project where `codd verify` has never passed, do not start by attempting to evolve; recommend `codd extract` + `codd fix` to establish a green baseline first

## Troubleshooting

- "I don't know which design doc is affected"
  - Read every doc under `docs/design/` and `docs/requirements/` and classify by frontmatter `modules` / topic
  - Use `codd impact` to compute downstream effects from any candidate doc
  - If still uncertain, ask the user one targeted question (not a list of 5)
- "Lexicon term is borderline new vs existing"
  - Treat as new. Always ask. The cost of asking is low; the cost of silent vocabulary drift is high
- "Verify keeps failing after retries"
  - Stop. Report which test, which file, which line. Let the user decide whether the design or the impl is wrong
- "User keeps adding requirements mid-execution"
  - Politely defer: "I'll finish this change first (estimated N minutes), then handle the next one"

## Output Format

When reporting back to the user, always include:

1. **Intent classification** — what kind of change you understood
2. **Files touched** — grouped by docs / source / tests
3. **Lexicon delta** — new / changed / deprecated terms (or "no changes")
4. **Verify status** — red 0 ✅ or red >0 with concrete failures
5. **Suggested commit message** — single line, conventional commits format
6. **Next action** — what you recommend the user do (review, commit, request more changes)

## Why This Skill Exists

CoDD's value is **coherence**: requirements, design, lexicon, source, and tests move together so no document lies about the system. The CLI form (`codd plan`, `codd implement`, `codd verify`) makes this explicit and reproducible — ideal for greenfield projects and CI automation.

But the CLI form has a cost in Brownfield modification: the user must remember which command to run, in which order, with what arguments. Each `codd fix "PHENOMENON"` invocation is a context switch.

`codd-evolve` removes that cost by accepting natural language ("add logout button") and orchestrating the CLI chain underneath. The user expresses intent; coherence is preserved automatically. The CLI remains the engine; this skill is the conversational front.

This is not a replacement for the CLI. Both are first-class:

- **CLI** for Greenfield, CI, automation, education, third-party orchestrators (e.g. multi-agent-shogun ashigaru)
- **Skill** for Brownfield, conversational modification, daily evolution within Claude Code

Use the right tool for the right phase.
