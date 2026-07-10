---
codd:
  node_id: plan:implementation-plan
  type: plan
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
    - module:errors
    reason: Error taxonomy (ExprError base + subtypes) must be implemented before
      tokenizer/parser/evaluator so every downstream module can depend on typed errors
      from the start.
  - targets:
    - module:parser
    reason: Out-of-scope features (variables, functions, exponentiation, bitwise ops,
      arbitrary precision, REPL, CLI, config files) must be explicitly excluded from
      the implementation sequence.
  modules:
  - tokenizer
  - parser
  - ast
  - evaluator
  - errors
  - api
---

# docs/plan/implementation_plan.md
