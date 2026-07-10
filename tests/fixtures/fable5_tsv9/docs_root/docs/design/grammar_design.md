---
codd:
  node_id: design:grammar-design
  type: design
  depends_on:
  - id: design:system-design
    relation: depends_on
    semantic: technical
  depended_by:
  - id: design:error-handling-design
    relation: depends_on
    semantic: technical
  - id: detailed_design:parser-ast-flow
    relation: depends_on
    semantic: technical
  - id: test:test-strategy
    relation: depends_on
    semantic: verification
  conventions:
  - targets:
    - module:parser
    reason: '* and / must bind tighter than + and -, and operators of equal precedence
      must be left-associative; violating this breaks the correctness invariant a
      - b - c == (a - b) - c.'
  - targets:
    - module:parser
    reason: Parenthesized and leading unary minus must be supported per requirement
      4; omitting it is a functional gap that blocks release.
  - targets:
    - module:parser
    reason: Grammar must remain closed to out-of-scope constructs (exponentiation,
      bitwise ops, functions, variables) to avoid silent scope creep.
  modules:
  - parser
  - ast
---

# docs/design/grammar_design.md
