---
codd:
  node_id: governance:error-taxonomy-decision
  type: governance
  depends_on:
  - id: req:exprcalc-requirements
    relation: derives_from
    semantic: governance
  depended_by:
  - id: design:system-design
    relation: constrained_by
    semantic: governance
  - id: design:error-handling-design
    relation: constrained_by
    semantic: governance
  - id: test:test-strategy
    relation: depends_on
    semantic: verification
  conventions:
  - targets:
    - module:errors
    reason: All public errors must derive from a single ExprError base type; introducing
      an error class outside this hierarchy is a release-blocking API contract violation.
  modules:
  - errors
---

# docs/governance/error_taxonomy_decision.md
