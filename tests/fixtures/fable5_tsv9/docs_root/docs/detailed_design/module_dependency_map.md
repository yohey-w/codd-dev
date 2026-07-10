---
codd:
  node_id: detailed_design:module-dependency-map
  type: design
  depends_on:
  - id: design:system-design
    relation: depends_on
    semantic: technical
  - id: detailed_design:tokenizer-flow
    relation: depends_on
    semantic: technical
  - id: detailed_design:parser-ast-flow
    relation: depends_on
    semantic: technical
  - id: detailed_design:evaluator-flow
    relation: depends_on
    semantic: technical
  depended_by:
  - id: plan:implementation-plan
    relation: depends_on
    semantic: technical
  - id: infra:build-ci-setup
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
    - module:errors
    - module:api
    reason: Module dependency direction must stay unidirectional (tokenizer -> parser
      -> evaluator, errors shared) with zero third-party or circular dependencies.
  modules:
  - tokenizer
  - parser
  - ast
  - evaluator
  - errors
  - api
  operation_flow:
    operations:
    - id: op-module-graph-acyclic
      actor: maintainer
      verb: review
      target: the src/*.ts import graph
      trigger: any pull request that adds, removes, or modifies an import statement
        under src/
      preconditions:
      - the changed file is one of src/tokenizer.ts, src/parser.ts, src/evaluator.ts,
        src/ast.ts, src/errors.ts, src/index.ts
      expected_outcomes:
      - src/tokenizer.ts imports only from src/errors.ts (and the standard library)
      - src/parser.ts imports only from src/tokenizer.ts, src/ast.ts, src/errors.ts
        (and the standard library)
      - src/evaluator.ts imports only from src/parser.ts, src/ast.ts, src/errors.ts
        (and the standard library)
      - src/ast.ts and src/errors.ts import nothing from src/tokenizer.ts, src/parser.ts,
        src/evaluator.ts, or src/index.ts
      - src/index.ts is not imported by any other src/*.ts file
      - no src/*.ts file imports any package other than a sibling src/*.ts module
      measurement_source: static review of import statements; optionally a Vitest-run
        static assertion in tests/module-graph.test.ts that parses each src/*.ts file's
        import declarations
      durable_state: none — this is a structural property of the source tree, not
        runtime state
      dod_obligations:
      - id: dod-graph-01
        text: grep of src/tokenizer.ts contains no import from './parser', './evaluator',
          or './ast'
      - id: dod-graph-02
        text: grep of src/evaluator.ts contains no import from './tokenizer'
      - id: dod-graph-03
        text: grep of src/ast.ts and src/errors.ts contains no import from any other
          src/*.ts file
      - id: dod-graph-04
        text: grep of src/tokenizer.ts, src/parser.ts, src/evaluator.ts, src/ast.ts,
          src/errors.ts contains no import from './index'
      - id: dod-graph-05
        text: grep of every file under src/ contains no import specifier other than
          a relative sibling path or a Node/TypeScript built-in
---

# docs/detailed_design/module_dependency_map.md
