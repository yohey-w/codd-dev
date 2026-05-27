---
codd:
  node_id: "design:coverage-obligation-driven-e2e-v0"
  type: design
  status: wip
  review_status: pending_shogun_lord_review
  depends_on:
    - id: "design:user-journey-coherence"
      relation: extends
    - id: "design:verify-architecture"
      relation: aligns_with
    - id: "design:cdp-browser-e2e"
      relation: aligns_with
    - id: "requirements:codd-requirements-v2"
      relation: implements
---

# Coverage-Obligation Driven E2E v0

Status: WIP design only. No implementation is proposed as complete in this document.

Review owner: Shogun / Lord.

## Implementation Status

cmd_494 first slice has implementation scaffolding for the obligation schema,
existing-declaration extractor, and `codd coverage-obligations` trace report CLI.
This is not a complete implementation of Coverage-Obligation Driven E2E.
`generated_e2e_candidates` and `selected_e2e_suite` remain future TODO items and
must not be counted as green coverage in this slice.

## Summary

Coverage-Obligation Driven E2E is the top-level concept. CoDD should not generate E2E
tests from routes, buttons, or isolated Persona-Journey examples alone. It should first
derive explicit `coverage_obligation` records from requirements, design docs, lexicon,
runtime constraints, static extraction, and existing verification declarations. Then it
should generate E2E candidates, select a minimal risk-aware suite, and emit a trace matrix
that shows every obligation as covered, delegated, waived, or uncovered.

Persona-Journey is therefore not a standalone first-class product concept. It is a subset:
`kind: role_sequence`, meaning "role x sequence obligation". A central administrator
journey, tenant administrator journey, or learner journey is valuable because it covers a
sequence of obligations under an actor, not because "journey" is separate from coverage.

## Non-Goals

- Do not implement this design in this phase.
- Do not add osato-lms names to CoDD core. osato-lms examples below are examples only.
- Do not add new DAG node kinds by default. Prefer existing `design_doc`, `expected`,
  `verification_test`, `plan_task`, and runtime report attributes unless implementation
  analysis later proves that a new kind is unavoidable.
- Do not weaken existing `global-action`, `action-outcome`, `crud-flow`, `connectivity`,
  `breakpoint-coverage`, or `e2e` checks. This design should coordinate them.

## External Research

| Area | Source | What CoDD adopts | Why |
| --- | --- | --- | --- |
| Model-Based Testing | ISTQB CT-MBT overview: https://istqb.org/certifications/certified-tester-model-based-tester-ct-mbt/ | Generate candidates from behavioral models: actors, states, actions, transitions, and selection criteria. | CoDD already has screen-flow extraction, user journey declarations, and verification templates. MBT gives the right abstraction for turning those into candidate paths instead of ad hoc test stubs. |
| Risk-Based Testing | ISTQB glossary: https://istqb-glossary.page/risk-based-testing/ | Assign `risk_level` and use it to prioritize selection depth, waiver strictness, and required evidence. | CoDD cannot run exhaustive E2E. Risk levels let high-impact obligations demand E2E while low-risk obligations can delegate to lower-level tests or require explicit waiver. |
| Combinatorial / t-way Testing | NIST ACTS FAQ: https://csrc.nist.gov/projects/automated-combinatorial-testing-for-software/faqs | Use t-way combinations for parameter axes such as role, breakpoint, auth state, data cardinality, locale/timezone, and runtime target. | NIST's interaction-rule framing supports covering important combinations without exhaustive enumeration. This maps directly to CoDD's role and breakpoint coverage problem. |
| Example Mapping / BDD | Cucumber Example Mapping: https://cucumber.io/docs/bdd/example-mapping/ and BDD overview: https://cucumber.io/docs/bdd/ | Capture story/rule/example/question shape as source material for obligations; unanswered questions become uncovered or blocked obligations, not silent gaps. | CoDD needs concrete examples, but the important artifact is the rule-to-example mapping. This prevents a single happy-path scenario from masquerading as full behavior coverage. |
| Playwright E2E practice | Playwright best practices: https://playwright.dev/docs/best-practices | Generated E2E should assert user-visible behavior, use resilient user-facing locators, isolate data/session state, run on CI, and fail on skipped coverage. | CoDD's selected E2E suite must be stable enough to serve as a gate. This source also supports sharding/parallelism and web-first assertions for reliable execution. |

Adoption boundary: external methods are adopted as design inputs, not as wholesale
framework dependencies. CoDD should express them in its own coverage vocabulary.

## Existing CoDD Alignment

| Existing concept | Current role | Coverage-obligation role |
| --- | --- | --- |
| `user_journeys` / C7 `user_journey_coherence` | Declares and checks browser-level user journeys through existing `design_doc` attributes. | Elevated into `kind: role_sequence` obligations. Missing role journeys become uncovered obligations unless explicitly waived. |
| `global-action` | Runtime category for global/session actions such as authenticated logout across breakpoints. | Becomes `kind: global_action` obligations, often cross-product with role and breakpoint axes. |
| `action-outcome` | Verifies a visible command reaches its intended outcome. | Becomes `kind: action_outcome` obligations with trigger, expected outcomes, side effects, and evidence. |
| `crud-flow` | Verifies mutation reflected in UI/API. | Becomes `kind: crud_flow` obligations. Some can delegate to API/component tests when browser behavior is not the risk. |
| `connectivity` | Verifies dev server, DB, and target availability. | Remains a runtime prerequisite obligation; failure blocks E2E evidence, not coverage completeness by itself. |
| `breakpoint-coverage` | Captures responsive layout obligations, especially mobile/desktop substitutions. | Becomes an axis for role-sequence and global-action obligations, with t-way strength by risk. |
| `e2e` | Existing runtime category / verification artifact. | Becomes one possible evidence type in `covered_by`; not the only valid coverage status. |
| `display_fields`, `presentation_specs`, `aggregation_policies` | C7-related declarative obligations for field display and aggregation. | Elevated into `kind: presentation_locale` and `kind: aggregation_policy` obligations. |
| `e2e_extractor.py` / `e2e_generator.py` | Extracts scenarios and renders framework-specific stubs. | Should feed `generated_e2e_candidates` after obligations exist, rather than being the source of truth. |
| `coverage_auditor.py` | Classifies coverage gaps as AUTO_ACCEPT / ASK / AUTO_REJECT. | Remains a decision aid, but the authoritative status is `coverage_status` on obligations. |
| `coverage_metrics.py` | Computes coverage ratios for design/test/CI/DAG completeness. | Can aggregate obligation statuses into trace and readiness metrics. |

## Naming

Recommended public term: Coverage-Obligation Driven E2E.

Recommended internal object: `coverage_obligation`.

Do not use "Persona-Journey coverage" as the top-level feature name. Use "Persona-Journey"
only when talking about the role-sequence subset.

## Concept Model

An obligation is an atomic behavior, constraint, evidence requirement, or sequence that
CoDD must account for. It may be covered by E2E, covered by a lower-level test, waived
with a reason and expiry, or uncovered.

Recommended obligation kinds:

| Kind | Meaning | Typical source |
| --- | --- | --- |
| `role_sequence` | Actor follows a meaningful 5-10 step workflow. | `design_doc.user_journeys`, stakeholder roles, requirements, screen-flow. |
| `action_outcome` | Visible command produces the promised outcome. | `operation_flow`, visible controls, action-outcome targets. |
| `global_action` | Global/session action exists and works across relevant contexts. | `runtime.global_action_targets`, auth requirements. |
| `breakpoint_coverage` | Behavior remains available on responsive breakpoints. | UI/layout requirements, breakpoint runtime targets. |
| `crud_flow` | Create/read/update/delete flow mutates and reflects state. | runtime CRUD targets, requirements. |
| `connectivity` | Runtime prerequisite is reachable and coherent. | runtime smoke categories. |
| `presentation_locale` | User-visible value uses the required format/locale/timezone. | `display_fields`, `presentation_specs`. |
| `aggregation_policy` | Multi-record display declares and proves aggregation semantics. | `aggregation_policies`. |
| `runtime_capability` | Deployment/runtime provides required capability. | `runtime_constraints`, `runtime_state`. |
| `lower_level_contract` | API/unit/component test intentionally owns the evidence. | tests, trace declarations. |

## Schema Proposal

Minimum schema:

```yaml
coverage_obligation:
  obligation_id: "obl:role_sequence:central_admin:tenant_lifecycle"
  source:
    type: "design_doc | requirement | lexicon | runtime | static | manual"
    ref: "docs/design/example.md#user_journeys[central_admin_tenant_lifecycle]"
  kind: "role_sequence"
  actor: "central_admin"
  goal: "Create and manage a tenant lifecycle from the admin console."
  preconditions:
    - "central_admin is authenticated"
    - "target tenant name is unique"
  expected_outcomes:
    - "tenant appears in the tenant list"
    - "tenant detail reflects saved values"
  side_effects:
    - "tenant record is created"
    - "audit event is emitted"
  risk_level: "P0 | P1 | P2 | P3"
  coverage_status: "covered_by_e2e | covered_by_lower_test | waived_with_reason_and_expiry | uncovered"
  covered_by:
    - type: "verification_test"
      ref: "tests/smoke/admin_tenant_lifecycle.spec.ts"
  waiver_reason: null
  waiver_expiry: null
```

Optional but recommended fields:

```yaml
  sequence_steps:
    - "open admin dashboard"
    - "open tenants"
    - "create tenant"
    - "confirm list/detail"
  risk_drivers:
    - "money_or_contract_impact"
    - "cross_role_access"
  pairwise_parameters:
    role: ["central_admin"]
    breakpoint: ["desktop", "mobile"]
    locale: ["ja-JP"]
    data_cardinality: ["zero", "one", "many"]
  evidence_signals:
    - "tenant_created_visible"
    - "audit_event_visible"
  tags:
    - "example:osato-lms"
  last_verified_at: null
```

Required field semantics:

- `obligation_id`: stable, unique, deterministic where possible.
- `source`: the earliest authoritative source and path/anchor that caused the obligation.
- `kind`: normalized vocabulary, not project-specific wording.
- `actor`: normalized role or `system` when no human actor exists.
- `goal`: concise user/business/runtime intent.
- `preconditions`: explicit setup needed for a valid test.
- `expected_outcomes`: user-visible or externally observable outcomes.
- `side_effects`: persisted, emitted, logged, or cross-service effects.
- `risk_level`: drives depth and candidate selection. Use project policy, but default to P0-P3.
- `coverage_status`: one of the four normalized values below.
- `covered_by`: evidence references when status is covered.
- `waiver_reason`: required only for waiver status.
- `waiver_expiry`: required only for waiver status.

## Coverage Status Semantics

Allowed statuses:

- `covered_by_e2e`: at least one E2E test or runtime verification covers the obligation.
- `covered_by_lower_test`: the obligation is intentionally delegated to unit/API/component/static
  tests and does not need E2E for current risk.
- `waived_with_reason_and_expiry`: a human-readable reason and future expiry date are present.
- `uncovered`: no valid coverage, delegation, or current waiver exists.

Incomplete states:

- `SKIP` is incomplete. A skipped E2E cannot produce `covered_by_e2e`.
- Implicit opt-out is incomplete. Absence of a journey, target, or status is not a waiver.
- Expired waiver is incomplete. It must revert to `uncovered`.
- "No user_journeys declared" is incomplete when roles, screens, actions, or requirements imply
  actor workflows.
- "Not applicable" must be represented as a waiver with reason and expiry until a stronger
  status model is deliberately designed.

## Pipeline

The pipeline is:

```text
requirements
  -> obligations
  -> generated_e2e_candidates
  -> selected_e2e_suite
  -> trace_matrix
```

### 1. requirements -> obligations

Inputs:

- Requirements and design frontmatter.
- `user_journeys`, runtime constraints, display fields, presentation specs, aggregation policies.
- Lexicon expected values and browser/runtime requirements.
- Static extraction from routes, screens, controls, forms, API handlers, role labels, and actions.
- Existing runtime targets: connectivity, CRUD, action outcome, global action, E2E.
- Coverage audit output and current coverage metrics.

Output:

- A normalized obligation matrix.
- A blind-spot list for inferred obligations with no declaration.

### 2. obligations -> generated_e2e_candidates

Candidate generation modes:

- Model-based paths: actor/state/action/transition graph produces role-sequence and action
  candidates.
- Rule/example paths: BDD or Example Mapping rules produce candidate examples.
- Runtime target expansion: global-action, action-outcome, CRUD, and breakpoint targets produce
  runnable candidate checks.
- t-way expansion: role, breakpoint, auth state, data cardinality, locale/timezone, and runtime
  target are combined to the configured strength.

Candidates are not automatically selected. Each candidate must declare which obligation IDs it
would cover.

### 3. generated_e2e_candidates -> selected_e2e_suite

Selection is a constrained set-cover problem:

- Universe: all obligations requiring E2E coverage.
- Candidate set: generated E2E candidates with `covers: [obligation_id...]`.
- Objective: cover all required obligations with minimal cost, weighted by risk.
- Constraints: test isolation, DB mutation serial rules, target runtime availability, flake risk,
  secrets hygiene, and execution budget.

### 4. selected_e2e_suite -> trace_matrix

Trace matrix rows must include:

- obligation ID and source.
- selected evidence or delegated lower-level evidence.
- current status.
- waiver fields when relevant.
- last verification artifact.
- owner for uncovered or expired-waiver rows.

## Candidate Selection Rules

### Set Cover

Choose the smallest suite that covers required E2E obligations, but do not optimize only for count.
The cost function should include runtime, setup cost, flake risk, DB mutation risk, and maintenance
cost.

Example scoring:

```text
score(candidate) =
  risk_weighted_new_obligations_covered
  - runtime_cost
  - mutation_isolation_cost
  - flake_risk
  - duplicate_penalty
```

### Duplicate Suppression

Suppress candidates when:

- Their `covers` set is a strict subset of a cheaper already selected candidate.
- They test the same actor/goal/outcome with only route wording differences.
- They rely on the same setup and assert no additional side effects.
- They differ only by selector style, not behavioral evidence.

Do not suppress when:

- Different breakpoints change navigation availability.
- Different roles change authorization or data visibility.
- Different cardinalities change aggregation behavior.
- Different runtime targets change deployment capability.

### Risk-Based Depth

Default depth:

| Risk | Required depth |
| --- | --- |
| P0 | E2E required unless explicit waiver; t-way strength 3+ for relevant axes; post-deploy gate. |
| P1 | E2E or strong lower-level delegation with at least one E2E integration path. |
| P2 | Lower-level delegation acceptable; E2E only if cross-boundary behavior is the risk. |
| P3 | Static/unit evidence or waiver acceptable when rationale is recorded. |

### t-way Strength

Default axis set:

- actor / role.
- breakpoint / viewport.
- auth state.
- data cardinality.
- locale / timezone / format.
- runtime target.
- feature flag / tenant configuration when declared.

Default strength:

- P0: 3-way for relevant axes, with manually pinned critical combinations.
- P1: 2-way plus role-specific critical combinations.
- P2/P3: pairwise only when parameter interaction is the known risk.

### Lower-Level Test Delegation

Use `covered_by_lower_test` when:

- The risk is pure calculation, parsing, formatting, or API contract behavior.
- Browser behavior adds little evidence.
- A lower-level test directly references the obligation and asserts the expected outcome.
- The trace matrix records the test reference.

Do not delegate when:

- Role, navigation, layout, browser session, deployment runtime, or cross-screen sequence is the
  defect class.
- The obligation was inferred from a prior E2E miss.
- The lower-level test does not assert the user-visible outcome.

## Undeclared Blind Spot Detection

CoDD should infer blind spots before generation:

| Signal | Blind spot | Required treatment |
| --- | --- | --- |
| Actor/role appears in requirements but no role-sequence obligation exists. | Missing role journey. | Create `uncovered` role-sequence obligation unless waived. |
| Route/screen has visible controls but no action-outcome obligation. | Button/link may be inert. | Create action-outcome candidate. |
| Runtime global action exists for desktop but no mobile/breakpoint declaration. | Responsive session action can disappear. | Create global-action + breakpoint obligations. |
| Collection display has count/average/latest language but no aggregation policy. | Misleading aggregate display. | Create aggregation-policy obligation. |
| Date/time/status display has locale-sensitive values but no presentation spec. | Raw or wrong locale display. | Create presentation-locale obligation. |
| `--runtime-skip`, no runtime target, or no declared journey. | Silent incomplete coverage. | Mark incomplete, never green. |
| Waiver expiry is past. | Stale exemption. | Revert to uncovered. |
| E2E generated from route list covers no requirement/source. | Unanchored test. | Keep as candidate only, not trace evidence. |

This changes the existing C7 "no user_journeys declared" skip behavior for projects that contain
actor, route, or requirement evidence. A truly actorless project may still have no role-sequence
obligations, but the absence must be derived, not assumed.

## osato-lms Example Scope

The following examples are deliberately project-specific examples. They must not become hardcoded
CoDD core vocabulary.

### Journey Samples

#### central_admin: tenant and course oversight

1. Log in as central administrator.
2. Open admin dashboard.
3. Open tenants.
4. Create or edit a tenant.
5. Open courses.
6. Assign or inspect delivery targets.
7. Confirm notification or audit signal.
8. Log out.

#### tenant_admin: tenant operation and learner progress

1. Log in as tenant administrator.
2. Open tenant dashboard.
3. Open staff or learner list.
4. Add or inspect a learner.
5. Open tenant courses.
6. Inspect progress with aggregation semantics.
7. Open notifications.
8. Log out.

#### learner: learning and completion path

1. Log in as learner.
2. Open learner dashboard.
3. Open course list.
4. Open lesson detail.
5. Start or continue lesson.
6. Submit progress or assessment.
7. Open notification detail.
8. Log out.

### Obligation Matrix Example

| obligation_id | kind | actor | goal | expected_outcomes | risk_level | coverage_status | covered_by | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| obl:osato:central_admin:login_dashboard | role_sequence | central_admin | Reach admin dashboard after login. | Dashboard visible; session established. | P0 | covered_by_e2e | tests/smoke/cmd491-login-codd-obligation-signals.spec.ts | Role-sequence start. |
| obl:osato:central_admin:tenant_create_visible | action_outcome | central_admin | Create tenant and see it in list/detail. | Tenant row/detail visible. | P0 | uncovered | null | Example uncovered row for future CRUD E2E. |
| obl:osato:central_admin:delivery_target_manage | crud_flow | central_admin | Add/edit/remove delivery target. | Mutation reflected; candidate list updates. | P0 | covered_by_e2e | tests/smoke/delivery-targets.spec.ts | From prior delivery-target smoke. |
| obl:osato:central_admin:audit_signal | lower_level_contract | central_admin | Admin mutation emits audit signal. | Audit record exists. | P1 | covered_by_lower_test | tests/api/audit-log.test.ts | Browser adds low value if UI has no audit screen. |
| obl:osato:central_admin:logout_mobile | global_action | central_admin | Log out on mobile. | Logout visible; protected route redirects after logout. | P0 | covered_by_e2e | tests/smoke/mobile_logout_smoke.spec.ts | Breakpoint-critical. |
| obl:osato:tenant_admin:login_dashboard | role_sequence | tenant_admin | Reach tenant dashboard after login. | Tenant dashboard visible. | P0 | covered_by_e2e | tests/smoke/cmd491-login-codd-obligation-signals.spec.ts | Role-sequence start. |
| obl:osato:tenant_admin:add_learner | action_outcome | tenant_admin | Add learner and see readback. | Learner appears in tenant list. | P0 | covered_by_e2e | tests/smoke/nonadmin-role-matrix.spec.ts | Cross-role tenant boundary. |
| obl:osato:tenant_admin:progress_aggregation | aggregation_policy | tenant_admin | See progress aggregate with source count. | Average and source count match fixture. | P0 | covered_by_e2e | tests/smoke/cmd491-nonadmin-presentation-signals.spec.ts | C7 aggregation obligation. |
| obl:osato:tenant_admin:notification_locale | presentation_locale | tenant_admin | See localized notification date/time. | ja-JP style date/time; no raw ISO. | P1 | covered_by_e2e | tests/smoke/cmd491-nonadmin-presentation-signals.spec.ts | C7 presentation obligation. |
| obl:osato:tenant_admin:logout_desktop | global_action | tenant_admin | Log out on desktop. | Sidebar/logout works; protected route redirects. | P0 | covered_by_e2e | tests/smoke/desktop_logout_smoke.spec.ts | Breakpoint pair with mobile. |
| obl:osato:learner:login_dashboard | role_sequence | learner | Reach learner dashboard after login. | Learner dashboard visible. | P0 | covered_by_e2e | tests/smoke/cmd491-login-codd-obligation-signals.spec.ts | Role-sequence start. |
| obl:osato:learner:course_list | role_sequence | learner | Open assigned courses. | Assigned course list visible. | P1 | covered_by_e2e | tests/smoke/nonadmin-role-matrix.spec.ts | Sequence continuation. |
| obl:osato:learner:lesson_progress | action_outcome | learner | Complete lesson progress action. | Progress persists after reload. | P0 | uncovered | null | Must not be accepted from click-only evidence. |
| obl:osato:learner:assessment_result | action_outcome | learner | Submit assessment and see result. | Result visible and persisted. | P1 | covered_by_lower_test | tests/api/assessment-submit.test.ts | Needs E2E only if UI path remains risky. |
| obl:osato:learner:notification_detail | role_sequence | learner | Open notification detail and return to dashboard. | Detail visible; navigation escape route exists. | P1 | waived_with_reason_and_expiry | null | Example waiver: covered next iteration; expiry 2026-06-30. |

Important: the final matrix must not allow `covered_by_e2e` unless the referenced test actually
asserts the outcome and reports zero skips.

### Selected E2E Suite Example

| selected_test | Covers obligations | Selection reason |
| --- | --- | --- |
| `tests/smoke/cmd491-login-codd-obligation-signals.spec.ts` | central_admin login, tenant_admin login, learner login | One isolated candidate covers the start of all three role sequences and core auth runtime. |
| `tests/smoke/cmd491-nonadmin-presentation-signals.spec.ts` | tenant progress aggregation, notification locale | High-risk prior miss; user-visible presentation/aggregation cannot be delegated. |
| `tests/smoke/mobile_logout_smoke.spec.ts` | mobile global action for all roles | Covers breakpoint-specific logout disappearance risk. |
| `tests/smoke/desktop_logout_smoke.spec.ts` | desktop global action for all roles | Complements mobile and guards layout substitution. |
| `tests/smoke/nonadmin-role-matrix.spec.ts` | tenant add learner, learner course list | Cross-role, tenant-scoped path; higher value than isolated route smoke. |

Excluded or delegated examples:

- Admin audit signal is delegated to a lower-level API/database test because the user-visible admin
  outcome is already covered and the audit UI is not the risk.
- Learner lesson progress remains uncovered in this example because click-only evidence would be
  false confidence.
- Notification detail has an explicit waiver with expiry; it must not count as covered after expiry.

## New Concepts vs Elevated Existing Concepts

New concepts:

- `coverage_obligation`: explicit record of a behavior, constraint, or evidence requirement.
- `obligation matrix`: normalized list of all obligations and their statuses.
- `coverage_status`: four-value status model that treats skip, implicit opt-out, and expired waiver
  as incomplete.
- `generated_e2e_candidates`: candidate tests with declared covered obligation IDs.
- `selected_e2e_suite`: risk-aware set-cover output, not every generated candidate.
- `trace_matrix`: final auditable map from source to obligation to evidence/status.

Elevated existing concepts:

- `user_journeys` become `role_sequence` obligations.
- `global-action`, `action-outcome`, `crud-flow`, `connectivity`, `breakpoint-coverage`, and `e2e`
  become obligation kinds or evidence categories under one coverage model.
- C7 presentation and aggregation checks become explicit obligations rather than special-case
  journey violations.
- `coverage_metrics.py` becomes an aggregation layer over obligation status.
- `coverage_auditor.py` remains a review and classification helper, but not the canonical status.
- `e2e_extractor.py` and `e2e_generator.py` become candidate producers, not coverage authority.

## Impact Surface

Requirements:

- Requirements and design docs should be able to declare obligations directly or indirectly through
  existing attributes.
- Questions discovered by Example Mapping or BDD discovery should become uncovered or blocked
  obligations, not disappear from the trace.

Lexicon:

- Expected values may carry evidence signals, browser requirements, presentation requirements, and
  aggregation signals.
- Lexicon should not encode project-specific route names into CoDD core.

Static extraction:

- Routes, screens, forms, buttons, role names, and transitions can infer candidate obligations.
- Static inference should create `uncovered` rows when no declaration exists, not auto-accept.

Runtime:

- Runtime smoke categories provide evidence for obligations.
- A skipped category is incomplete.
- Target health/connectivity is prerequisite evidence and should not be confused with behavioral
  coverage.

Generation:

- E2E generation should consume selected candidates and output tests that assert user-visible
  outcomes with stable locators and web-first assertions.

Reporting:

- Coverage reports should show both percentage and blocking rows.
- The useful gate is not "E2E generated", but "no P0/P1 obligation is uncovered, skipped, or expired".

## Alternatives Considered

| Alternative | Pros | Cons | Decision |
| --- | --- | --- | --- |
| Persona-Journey as the top-level concept | Easy to explain to humans; aligns with role workflows. | Misses action-outcome, global-action, aggregation, presentation, and lower-level delegation. | Reject as top-level; keep as role-sequence subset. |
| Route-driven E2E generation | Simple and already close to existing extractor/generator. | Produces unanchored tests and misses obligations that are not routes. | Reject as coverage authority; keep as candidate input. |
| Obligation-first model | Unifies existing checks, traceability, risk, selection, and waiver semantics. | Requires schema, status normalization, and selection logic. | Recommend. |

## Open Questions for Review

- Should `waived_with_reason_and_expiry` be the only non-covered exemption, or should a fifth
  `not_applicable_with_reason` status exist later?
- Should obligation IDs be generated from source paths, semantic names, or both?
- What is the default risk policy for projects without explicit P0-P3 annotations?
- Should selected E2E suite output be committed as a manifest, or generated during verify?
- How strict should the first implementation be when legacy projects have many implicit opt-outs?

## Acceptance for This Design Phase

This WIP design satisfies the design-phase intent when:

- Coverage-Obligation Driven E2E is the main concept.
- Persona-Journey is explicitly scoped to `role x sequence` obligations.
- External research is cited with URLs and adaptation decisions.
- The schema contains required fields and normalized statuses.
- The pipeline is defined from requirements to trace matrix.
- Candidate selection includes set cover, duplicate suppression, risk-based depth, t-way strength,
  and lower-level test delegation.
- Undeclared blind spots are treated as incomplete coverage.
- osato-lms is used only as an example.
- Implementation is deferred until Shogun/Lord review.
