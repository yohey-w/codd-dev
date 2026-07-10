---
codd:
  node_id: infra:build-ci-setup
  type: document
  depends_on:
  - id: detailed_design:module-dependency-map
    relation: depends_on
    semantic: technical
  depended_by:
  - id: test:test-strategy
    relation: depends_on
    semantic: verification
  conventions:
  - targets:
    - module:api
    reason: CI must enforce zero third-party runtime dependencies (a dependency-manifest
      check) since the requirement mandates a dependency-free library.
  - targets:
    - module:api
    reason: Build/test pipeline must verify purity and determinism (no I/O, no global
      state, no network, no time) as an automated, release-blocking gate.
---

# docs/infra/build_ci_setup.md
