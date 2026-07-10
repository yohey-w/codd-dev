---
codd:
  node_id: design:public-api-design
  type: design
  depends_on:
  - id: design:system-design
    relation: depends_on
    semantic: technical
  depended_by:
  - id: design:error-handling-design
    relation: depends_on
    semantic: technical
  - id: test:test-strategy
    relation: depends_on
    semantic: verification
  conventions:
  - targets:
    - module:ast
    reason: Parsed AST nodes must be immutable value objects exposing operator/operands
      or literal value, so callers can introspect structure without evaluating.
  - targets:
    - module:api
    reason: tokenize(text), parse(tokens|text), and evaluate(text) signatures are
      the sole public surface; no CLI, network, or config-file entry points may be
      added (out of scope).
  - targets:
    - module:errors
    reason: Every public function must guarantee that invalid input surfaces as a
      typed ExprError subtype, never an untyped runtime exception.
  modules:
  - api
  - ast
  operation_flow:
    operations:
    - id: op-api-tokenize
      actor: consumer
      verb: call
      target: tokenize(text)
      trigger: consumer imports { tokenize } from src/index.ts and invokes it with
        a string
      preconditions:
      - text is a string value (compile-time enforced by the TypeScript signature)
      expected_outcomes:
      - returns Token[] on lexically valid input
      - throws LexError(character, position) on the first unrecognized character
      forbidden_actors:
      - no CLI, no network listener, no config-file loader may invoke tokenize as
        an entry point other than direct function call
      measurement_source: return value / thrown error of the tokenize() call
      durable_state: none
      dod_obligations:
      - id: dod-api-tokenize-01
        text: tokenize is the only exported lexical-analysis entry point in src/index.ts
      - id: dod-api-tokenize-02
        text: tokenize('2 $ 3') throws an instance of LexError, and err instanceof
          ExprError is also true
    - id: op-api-parse
      actor: consumer
      verb: call
      target: parse(input)
      trigger: consumer invokes parse with either a raw string or a Token[] from a
        prior tokenize() call
      preconditions:
      - input is a string, or a Token[] produced by tokenize
      expected_outcomes:
      - returns a frozen ExprNode graph on grammatically valid input
      - throws ParseError(expected) on malformed input, with no partial AST returned
      - every node in the returned graph satisfies Object.isFrozen(node) === true,
        including nested children
      measurement_source: return value / thrown error of the parse() call, and Object.isFrozen()
        on returned nodes
      durable_state: none
      readback: a caller re-reading node.type / node.operator / node.value / node.left
        / node.right observes the same values on every access, since nodes are frozen
        and never mutated post-construction
      dod_obligations:
      - id: dod-api-parse-01
        text: parse(tokenize('2 + 3')) and parse('2 + 3') produce structurally deep-equal
          ExprNode graphs
      - id: dod-api-parse-02
        text: two separate parse('2 + 3') calls return referentially distinct (!==)
          but deep-equal root nodes
      - id: dod-api-parse-03
        text: attempting node.operator = '*' on any returned node either throws (strict
          mode) or leaves node.operator unchanged
      - id: dod-api-parse-04
        text: every ExprNode field (operator, left, right, value, operand) is readable
          via direct property access with no method call
    - id: op-api-evaluate
      actor: consumer
      verb: call
      target: evaluate(input)
      trigger: consumer invokes evaluate with either a raw string or an ExprNode from
        a prior parse() call
      preconditions:
      - input is a string, or an ExprNode produced by parse
      expected_outcomes:
      - returns a number on successful evaluation
      - throws DivisionByZeroError when a division node's right operand evaluates
        to 0
      measurement_source: return value / thrown error of the evaluate() call
      durable_state: none
      dod_obligations:
      - id: dod-api-evaluate-01
        text: evaluate(parse('2 + 3 * 4')) === evaluate('2 + 3 * 4') === 14
      - id: dod-api-evaluate-02
        text: evaluate('10 / 0') throws DivisionByZeroError and never returns Infinity,
          -Infinity, or NaN
      - id: dod-api-evaluate-03
        text: calling evaluate on a pre-parsed ExprNode does not re-invoke tokenize
          or parse (verified by absence of re-tokenization side effects — no observable
          difference in outcome, only in call count when instrumented in a test double)
    - id: op-api-error-boundary
      actor: consumer
      verb: catch
      target: any error thrown across tokenize/parse/evaluate
      trigger: any invalid-input call to a public function
      preconditions:
      - input triggers a lex, parse, or division-by-zero failure at any stage, including
        transitively (e.g. evaluate(string) failing during its internal parse step)
      expected_outcomes:
      - the thrown value is always an instance of ExprError, and of exactly one of
        LexError, ParseError, DivisionByZeroError
      forbidden_actors:
      - no public function throws a bare Error, TypeError, or RangeError to the caller
      measurement_source: instanceof checks in Vitest tests against the caught error
      durable_state: none
      dod_obligations:
      - id: dod-api-errbound-01
        text: evaluate('1 +') throws ParseError (propagated from its internal parse
          call), not a generic Error
      - id: dod-api-errbound-02
        text: evaluate('2 $ 3') throws LexError (propagated from its internal tokenize
          call), not a generic Error
      - id: dod-api-errbound-03
        text: no test observes err.constructor.name === 'Error' for any invalid input
          across the three public functions
---

# docs/design/public_api_design.md
