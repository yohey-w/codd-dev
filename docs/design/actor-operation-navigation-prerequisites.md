# Actor Operation Navigation Prerequisites

## Problem

CoDD can derive operational E2E scenarios from `operation_flow`, but an implementation can still pass tests that open a deep link or call an API directly while the actor-facing path to that operation is missing.

This creates a false green state: the operation exists technically, but the intended actor cannot discover or reach it through the product surface.

## Generic Rule

For every actor-facing operation with a parameterized non-API route, CoDD treats route reachability as part of the operation contract.

Examples of parameterized actor-facing routes:

- `/records/:recordId/edit`
- `/projects/:projectId/tasks/:taskId`
- `/accounts/[accountId]/billing`

The rule is framework-neutral: it applies to any user-facing surface with an object-specific route, not only web apps.

## Design Contract

Operational Behavior Models should record both the operation and how the actor reaches it:

- actor
- verb
- target
- route or surface
- trigger
- preconditions
- expected outcomes
- navigation or entry surface when the route is object-specific

When requirements or designs describe user-facing surfaces, role-specific actions, navigation, or object-specific screens, the planner must assign an artifact responsibility for this actor operation model before implementation planning.

## E2E Contract

CoDD generates a `navigation_prerequisite` operational scenario for parameterized actor-facing routes. Passing evidence must prove:

- scenario state exists without trusting stale seed data
- the actor starts from an actor-facing entry/list/parent surface
- the actor uses a visible control or navigation affordance
- the expected route/content is reached
- direct URL navigation or lower-layer API calls alone are insufficient

This complements existing axes such as `happy_path`, `persistence_readback`, and `derived_state_chain`.

## Non-Goals

- CoDD must not hardcode framework routes, selectors, or domain nouns.
- CoDD must not assume Playwright is the only runner.
- CoDD must not require every static informational page to have a navigation prerequisite; the rule is scoped to object-specific actor-facing operation routes.
