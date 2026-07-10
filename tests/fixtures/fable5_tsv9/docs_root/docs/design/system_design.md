---
codd:
  node_id: design:system-design
  type: design
  depends_on:
  - id: test:acceptance-criteria
    relation: constrained_by
    semantic: governance
  - id: governance:error-taxonomy-decision
    relation: constrained_by
    semantic: governance
  depended_by:
  - id: design:grammar-design
    relation: depends_on
    semantic: technical
  - id: design:public-api-design
    relation: depends_on
    semantic: technical
  - id: detailed_design:module-dependency-map
    relation: depends_on
    semantic: technical
  - id: test:test-strategy
    relation: depends_on
    semantic: verification
  conventions:
  - targets:
    - module:tokenizer
    - module:parser
    - module:evaluator
    - module:api
    reason: No third-party dependencies are permitted; only the language standard
      library may be used across every module.
  - targets:
    - module:tokenizer
    - module:parser
    - module:evaluator
    reason: The library must be pure and deterministic — no global state, no I/O,
      no network, no time — for every module in the pipeline.
  - targets:
    - module:parser
    reason: Scope is bounded to +, -, *, /, parentheses, and unary minus; variables,
      functions, exponentiation, bitwise operators, and arbitrary precision are explicitly
      out of scope and must not leak into the architecture.
  modules:
  - tokenizer
  - parser
  - evaluator
  - errors
  - api
---

# docs/design/system_design.md
