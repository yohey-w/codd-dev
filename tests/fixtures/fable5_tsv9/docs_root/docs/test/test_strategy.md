---
codd:
  node_id: test:test-strategy
  type: test
  depends_on:
  - id: req:exprcalc-requirements
    relation: derives_from
    semantic: governance
  - id: test:acceptance-criteria
    relation: depends_on
    semantic: verification
  - id: governance:error-taxonomy-decision
    relation: depends_on
    semantic: verification
  - id: design:system-design
    relation: depends_on
    semantic: verification
  - id: design:grammar-design
    relation: depends_on
    semantic: verification
  - id: design:public-api-design
    relation: depends_on
    semantic: verification
  - id: design:error-handling-design
    relation: depends_on
    semantic: verification
  - id: detailed_design:tokenizer-flow
    relation: depends_on
    semantic: verification
  - id: detailed_design:parser-ast-flow
    relation: depends_on
    semantic: verification
  - id: detailed_design:evaluator-flow
    relation: depends_on
    semantic: verification
  - id: detailed_design:module-dependency-map
    relation: depends_on
    semantic: verification
  - id: plan:implementation-plan
    relation: depends_on
    semantic: verification
  - id: infra:build-ci-setup
    relation: depends_on
    semantic: verification
  depended_by: []
  conventions: []
  modules:
  - api
  - ast
  - errors
  - evaluator
  - parser
  - tokenizer
---

# docs/test/test_strategy.md
