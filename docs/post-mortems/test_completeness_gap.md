# Post-mortem — Test Completeness Gap (CoDD)

Author: gunshi (軍師)
Date: 2026-05-10
Source: cmd_462 Phase A
Status: Root cause identified, design doc separate (`queue/reports/gunshi_462_test_completeness_design.yaml`)

---

## TL;DR

CoDD shipped a project to deploy with **two latent test-completeness gaps**:

1. **User-journey E2E was never generated** for several first-class user-facing flows.
2. **Tests existed but the CI pipeline that runs them was failing for six days**, and `codd dag verify` never noticed.

Both gaps are CoDD design-level structural defects. The C7 (`user_journey_coherence`) and C6 (`deployment_completeness`) checks are correctly implemented, but they only verify **what was already declared**. They do not proactively require the declarations themselves, and they do not consult any signal about whether CI is actually executing.

The fixes belong in the elicit/check layer (Layer A core) and in one optional new lexicon plug-in (Layer C). No project-specific patching is needed.

---

## Defect 1 — User-Journey E2E Not Generated

### What happened

A multi-actor system reached deploy with no E2E test for several primary user journeys (e.g. "actor-A completes a course", "actor-B creates a content unit", "actor-C provisions a tenant", "actor-D pays via payment provider"). The journeys are described in prose throughout the requirements/design, but they were never structurally declared.

### Direct evidence (CoDD core)

`codd/dag/checks/user_journey_coherence.py:65-76`:

```python
journey_docs = [
    node
    for node in sorted(target_dag.nodes.values(), key=lambda item: item.id)
    if node.kind == "design_doc" and self._journey_entries(node)
]
if not journey_docs:
    return UserJourneyCoherenceResult(
        severity="info",
        status="pass",
        message="No user_journeys declared, C7 SKIP",
        block_deploy=self.block_deploy,
    )
```

Behavior: if **no** `design_doc` declares `user_journeys:` in its frontmatter, C7 returns **status=pass / severity=info**.

`codd/elicit/`: no module references `user_journey`, `persona`, or `test_strategy`. The elicit engine never asks "for each actor in this system, list their journeys".

`codd_plugins/lexicons/`: 38 lexicons today. None has a coverage axis whose semantics are "for every actor, an end-to-end journey × an E2E verification must exist". The closest neighbour, `process_test_iso29119`, covers test concepts / processes / documentation / techniques / keyword-driven specs — not the actor-to-journey-to-E2E matrix.

`codd/templates/` (design and plan templates): `user_journeys:` is an optional frontmatter key. There is no required field that surfaces the journey concept to authors.

### Root cause (design level)

C7 is a **contingent** check, not a **proactive** one:

| Layer | What exists | What is missing |
| --- | --- | --- |
| elicit | Asks for coverage by lexicon axes (a11y, security, data, …). | Does not ask "for each actor / role you discovered, list their journeys". |
| Templates | Allow `user_journeys:` frontmatter. | Do not mandate it. |
| C7 check | Verifies coherence of declared journeys (lexicon ref → plan task → E2E test → post-deploy). | Treats *absence of declaration* as PASS, not as a violation. |

The structural pattern is: **"verify what is declared, don't enforce that important things are declared"**. The same shape will repeat for any future first-class concept (incident playbook, data subject request flow, admin runbook) unless the elicit layer is taught to require declaration.

### Why no other check caught it

- `coverage_gate`, `dag_completeness`, `deployment_completeness`, `drift` — none of these consume the actor/role concept. They reason about node-kind balance and edge wiring, not about whether the human-meaningful flows exist.
- BABOK lexicon does have a `Stakeholder roles` axis, so actors *are* surfaced during elicit, but the elicit engine then drops them: there is no findings type "actor X has no declared journey".

---

## Defect 2 — Tests Exist But CI Is Not Running Them

### What happened

The project has Vitest unit tests, Playwright E2E tests, and an `Integration Tests` workflow. After the most recent deploy push, `Integration Tests` failed in CI and remained failing for six days. No new push, no rerun, and no CoDD signal that CI was unhealthy. `codd dag verify` reported PASS the entire time.

### Direct evidence (CoDD core)

`codd/dag/checks/deployment_completeness.py` (C6) verifies a static chain:

```
design_doc → deployment_doc → impl_file → runtime_state → verification_test
... and verification_test is reachable from deployment_doc.post_deploy
```

It uses `_verification_test_in_deploy_flow` to check that the test artifact is *referenced* somewhere in `deploy.yaml.post_deploy`. It never asks "did the most recent CI run on `main` succeed?".

`codd/deployer.py:226-233` lists every gate the deploy preflight collects:

```python
_collect_validate_gate(...)
_collect_drift_gate(...)
_collect_coverage_gate(...)
_collect_dag_completeness_gate(...)
_collect_deployment_completeness_gate_result(...)
_collect_user_journey_coherence_gate_result(...)
```

There is **no `_collect_ci_health_gate(...)`**.

`codd/generator.py:543-549`: the LLM design-doc prompt includes a `## CI/CD Pipeline Generation Meta-Prompt` section instructing the LLM to emit `.github/workflows/ci.yml` with a `// @generated-by: codd propagate` marker. So CoDD *can* generate a workflow file — but:

- whether it was actually emitted depends on the LLM following the prompt;
- there is no check that the file is committed;
- there is no check that the file's `on:` triggers cover `push` / `pull_request` / scheduled runs;
- there is no check that the most recent run on `main` is green.

`codd_plugins/lexicons/ops_cicd_pipeline/lexicon.yaml`: covers GitOps principles (declarative config, version control, automated apply, continuous reconciliation, drift detection, rollback, observability). None of the axes addresses "CI run history" or "trigger configuration". `dora_sre_metrics` mentions deployment frequency and change failure rate but does not enforce a freshness/success budget on CI runs.

### Root cause (design level)

CoDD treats CI/CD as a **deliverable artifact** ("the workflow file exists / the test file exists") rather than as a **runtime contract** ("CI runs on every change to `main` and is currently green"). The DAG view of the world is structural; CI execution is a temporal signal that the DAG-only model cannot represent.

Three sub-causes:

1. **No workflow-file static check.** Even if `generator.py` instructs the LLM to emit `ci.yml`, the resulting file is not validated for presence, triggers, or job structure.
2. **No CI run-history check.** The deploy gate collection does not query the CI provider for "latest run on default branch = success".
3. **No drift signal between local and remote `.github/`.** `codd diff` reports drift on requirements vs implementation, not on CI configuration vs operational reality.

### Why no other check caught it

- `deployment_completeness` (C6): static chain; passes if test file exists and `post_deploy` references it.
- `validate_gate`, `drift_gate`: neither inspects `.github/workflows/` or remote run state.
- Lexicons `ops_cicd_pipeline`, `dora_sre_metrics`: surface gap *findings* during elicit, but only if the requirements text already discusses CI; they do not block deploy.

---

## Impact assessment

| Dimension | Impact |
| --- | --- |
| Confirmed projects affected | The dogfood project where the gap surfaced. |
| Potentially affected | Any CoDD project with multiple actors and weak human attention to `user_journeys:` frontmatter; any CoDD project where CI was set up once and then left to drift. |
| Severity | High — a CoDD-green deploy decision can ship a system whose primary user flows are untested **and** whose CI pipeline is silently broken. |
| Detectability today | Only by manual audit. No `codd` command surfaces either gap. |

---

## Why this matters for CoDD's North Star

CoDD's positioning is "write functional requirements + constraints, get an automatically generated, verified, and repaired system". The implicit promise is that "verified" includes "the things a user actually does are tested" and "the tests are running". Both gaps break that promise without raising a single warning, which is the single worst failure mode for a coherence-driven tool: silent green.

---

## Fix design

See `queue/reports/gunshi_462_test_completeness_design.yaml`. Summary:

- **Defect 1 fix**: elicit-layer findings type `missing_journey_for_actor` + C7 amber promotion when actors exist but journeys do not. Optional Layer C lexicon `process_user_journey_completeness` for projects that want the full coverage matrix.
- **Defect 2 fix**: new C8 check `ci_health` with two modes — static (workflow file present + correct triggers) and runtime (latest run on default branch is success), runtime mode gated on config so air-gapped projects are not penalised. Wire into `_collect_ci_health_gate(...)` in `codd/deployer.py`.

Generality Gate constraints (verified during design):

- No project-specific naming, no Next.js / Prisma / LMS / payment-provider literals.
- Applicable surfaces: web (SPA), CLI, mobile, API server, ML model card project, batch pipeline. Verified by mapping each fix to those surface kinds in the design doc.

---

## Lessons captured for future CoDD design work

1. **A "verify if declared" check needs a paired "require declaration" gate** for any concept that is structurally important. C7 alone is insufficient; the same will be true for any future first-class concept.
2. **DAG verification is structural; runtime contracts need their own gate.** CI health, scheduled jobs, retention policy enforcement, secret rotation — none of these can be modelled purely as DAG nodes/edges; they need gates that consult external state.
3. **Generator output ≠ enforced output.** When `generator.py` instructs the LLM to emit a file, a corresponding check must verify the file is actually present and well-formed.
