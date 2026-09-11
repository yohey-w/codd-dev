---
codd:
  node_id: test:test-strategy
  type: test
  depends_on: []
---

# Test strategy — verifiable behavior registry

This document is the single canonical owner of the verifiable-behavior (VB) id
namespace for CoDD itself. Every behavior is declared here exactly once, and is
proved by a test carrying a `codd: covers vb=<id>` marker.

The rows are derived from the acceptance criteria in `docs/requirements/`
(`codd acceptance sync`), so the registry and the acceptance list cannot drift
apart. Edit the wording freely; keep the ids.

| VB ID | Verifiable behavior (from the acceptance criterion) | Requirement |
| --- | --- | --- |
| VB-AC-ACC-1 | A brownfield project with acceptance criteria, no verified_by column and no VB registry reports amber findings (including the missing registry) and still passes | AC-ACC-1 |
