---
codd:
  node_id: detailed_design:parser-ast-flow
  type: design
  depends_on:
  - id: design:grammar-design
    relation: depends_on
    semantic: technical
  - id: design:error-handling-design
    relation: depends_on
    semantic: technical
  depended_by:
  - id: test:test-strategy
    relation: depends_on
    semantic: verification
  conventions:
  - targets:
    - module:parser
    reason: Precedence climbing / recursive descent flow must implement left-associativity
      and correct binding of * / over + - exactly as specified in grammar design.
  - targets:
    - module:ast
    reason: AST node hierarchy must remain immutable value objects per the public
      API contract.
  - targets:
    - module:errors
    reason: Every malformed-input branch in the parse flow must terminate in a ParseError
      describing the expectation, not a generic exception.
  modules:
  - parser
  - ast
  - errors
---

# docs/detailed_design/parser_ast_flow.md
