---
codd:
  node_id: detailed_design:evaluator-flow
  type: design
  depends_on:
  - id: design:error-handling-design
    relation: depends_on
    semantic: technical
  depended_by:
  - id: test:test-strategy
    relation: depends_on
    semantic: verification
  conventions:
  - targets:
    - module:evaluator
    reason: Evaluation must be pure and deterministic and must raise DivisionByZeroError
      on division by zero rather than returning Infinity/NaN.
  - targets:
    - module:evaluator
    reason: Unary minus evaluation must be correct for both leading and parenthesized
      forms (evaluate("-3 + 4") == 1, evaluate("-(2 + 3)") == -5).
  modules:
  - evaluator
  - ast
  - errors
  operation_flow:
    operations:
    - id: op-eval-happy-path
      actor: consumer
      verb: call
      target: evaluate(text) returning a number
      trigger: consumer calls evaluate with lexically valid, grammatically valid text
        whose AST contains no Binary('/') node with a zero-valued right operand
      preconditions:
      - text tokenizes without LexError
      - text parses without ParseError
      - no division node in the resulting ExprNode evaluates its right operand to
        0
      expected_outcomes:
      - evaluate returns a number, not a Promise, not undefined, not a thrown value
      - the returned number equals the exact arithmetic result of the expression under
        standard operator precedence (unary minus tightest, then '*'/'/' left-to-right,
        then '+'/'-' left-to-right)
      - two calls to evaluate with the same text string return the same number
      measurement_source: return-value equality assertions in Vitest tests (tests/evaluator.test.ts)
      durable_state: none
      dod_obligations:
      - id: dod-eval-happy-01
        text: evaluate('-3 + 4') === 1
      - id: dod-eval-happy-02
        text: evaluate('-(2 + 3)') === -5
      - id: dod-eval-happy-03
        text: 'evaluate(''2 * 3 + 4'') === 10 (precedence: ''*'' binds tighter than
          ''+'')'
      - id: dod-eval-happy-04
        text: evaluate(text) called twice with the same text returns the identical
          number both times
    - id: op-eval-division-guard
      actor: consumer
      verb: call
      target: evaluate(text) reaching a Binary('/') node whose evaluated right operand
        is 0
      trigger: evalNode's Binary case dispatches to the '/' branch and the already-evaluated
        right operand equals 0
      preconditions:
      - text is grammatically valid
      - the ExprNode contains at least one Binary('/') node
      - that node's right subtree evaluates to numeric 0
      expected_outcomes:
      - the native '/' operator is never executed on the zero-valued pair
      - evalNode throws DivisionByZeroError before returning any numeric value from
        that call frame
      - evaluate never returns Infinity, -Infinity, or NaN for this input
      threshold:
      - right operand numerically equal to 0 (not merely close to 0; no epsilon tolerance
        is applied, per design:grammar-design's integer/decimal numeric literal scope)
      measurement_source: instanceof checks and return-value inspection in Vitest
        tests
      durable_state: none
      dod_obligations:
      - id: dod-eval-div-01
        text: evaluate('10 / 0') throws DivisionByZeroError
      - id: dod-eval-div-02
        text: evaluate('0 / 0') throws DivisionByZeroError, not NaN
      - id: dod-eval-div-03
        text: evaluate('1 / (2 - 2)') throws DivisionByZeroError (right operand is
          itself a computed subtree evaluating to 0, not a literal 0)
    - id: op-eval-unary-minus-forms
      actor: consumer
      verb: call
      target: evaluate(text) where text contains a leading unary minus, a parenthesized
        unary minus, or both
      trigger: evalNode visits a Unary node during the tree walk
      preconditions:
      - text is grammatically valid and contains at least one Unary node
      expected_outcomes:
      - evalNode negates the fully-evaluated result of the Unary node's single operand
        subtree, regardless of whether that operand is a Number leaf or a nested Binary
        subtree
      - a Unary node wrapping a Binary subtree evaluates that subtree completely before
        negation is applied
      boundary_cases:
      - unary minus applied directly to a number literal (e.g. "-3 + 4")
      - unary minus applied to a fully parenthesized binary expression (e.g. "-(2
        + 3)")
      measurement_source: return-value equality assertions in Vitest tests
      durable_state: none
      dod_obligations:
      - id: dod-eval-unary-01
        text: evaluate('-3 + 4') === 1 (leading unary minus on a Number leaf)
      - id: dod-eval-unary-02
        text: evaluate('-(2 + 3)') === -5 (unary minus on a parenthesized Binary subtree)
    - id: op-eval-purity
      actor: consumer
      verb: call
      target: evaluate(text) under repeated or concurrent invocation
      trigger: any call to evaluate, regardless of prior calls
      preconditions:
      - none (applies to every call)
      expected_outcomes:
      - evaluate performs no I/O and reads no ambient/non-deterministic state
      - evaluate does not mutate the ExprNode graph produced by parse
      - no module-level mutable variable in src/evaluator.ts persists state across
        separate evaluate calls
      forbidden_actors:
      - src/evaluator.ts does not import or call Date.now(), Math.random(), or process.env
      - evalNode does not reassign any field on a node object it visits
      measurement_source: static review of src/evaluator.ts for ambient-state reads/mutations;
        repeated-call equality assertions in Vitest tests
      durable_state: none
      dod_obligations:
      - id: dod-eval-purity-01
        text: calling evaluate('2 + 3') 1000 times in a loop returns 5 on every call
      - id: dod-eval-purity-02
        text: grep of src/evaluator.ts contains no reference to Date.now, Math.random,
          or process.env
---

# docs/detailed_design/evaluator_flow.md
