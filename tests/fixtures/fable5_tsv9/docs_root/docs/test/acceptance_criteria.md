---
codd:
  node_id: test:acceptance-criteria
  type: test
  depends_on:
  - id: req:exprcalc-requirements
    relation: derives_from
    semantic: governance
  depended_by:
  - id: design:system-design
    relation: constrained_by
    semantic: governance
  - id: test:test-strategy
    relation: depends_on
    semantic: verification
  conventions:
  - targets:
    - module:tokenizer
    - module:parser
    - module:evaluator
    reason: Precedence, associativity, and left-associativity invariants (a - b -
      c == (a - b) - c) are functional correctness requirements that gate release.
  - targets:
    - module:errors
    reason: Every invalid-input path must raise a typed ExprError subtype (LexError,
      ParseError, DivisionByZeroError); untyped/unstructured exceptions are release-blocking
      defects.
  - targets:
    - module:evaluator
    reason: Division by zero must raise DivisionByZeroError, not produce Infinity/NaN
      or an unstructured crash.
  modules:
  - tokenizer
  - parser
  - evaluator
  - errors
---

# docs/test/acceptance_criteria.md
