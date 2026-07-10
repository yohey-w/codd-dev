---
codd:
  node_id: design:error-handling-design
  type: design
  depends_on:
  - id: design:grammar-design
    relation: depends_on
    semantic: technical
  - id: design:public-api-design
    relation: depends_on
    semantic: technical
  - id: governance:error-taxonomy-decision
    relation: constrained_by
    semantic: governance
  depended_by:
  - id: detailed_design:tokenizer-flow
    relation: depends_on
    semantic: technical
  - id: detailed_design:parser-ast-flow
    relation: depends_on
    semantic: technical
  - id: detailed_design:evaluator-flow
    relation: depends_on
    semantic: technical
  - id: test:test-strategy
    relation: depends_on
    semantic: verification
  conventions:
  - targets:
    - module:errors
    - module:tokenizer
    reason: LexError must name the offending character and its position; message contract
      is release-blocking for debuggability.
  - targets:
    - module:errors
    - module:parser
    reason: ParseError must describe what was expected at the point of failure (e.g.
      for `1 +`, `(1 + 2`, `1 2`); a generic message violates the requirement.
  - targets:
    - module:errors
    - module:evaluator
    reason: DivisionByZeroError is a distinct, required error type for division by
      zero — it must not be conflated with ParseError or a native exception.
  - targets:
    - module:errors
    reason: All errors must derive from the single ExprError base type established
      in the error taxonomy decision.
  modules:
  - errors
  - tokenizer
  - parser
  - evaluator
  operation_flow:
    operations:
    - id: op-error-lex-contract
      actor: consumer
      verb: catch
      target: LexError thrown by tokenize(text) or by parse/evaluate's internal tokenize
        call
      trigger: consumer calls tokenize, parse, or evaluate with text containing an
        unrecognized character
      preconditions:
      - text contains at least one character not mapped to number, +, -, *, /, (,
        ) or whitespace
      expected_outcomes:
      - thrown value is instanceof LexError and instanceof ExprError
      - err.character equals the exact offending character
      - err.position equals the zero-based index of that character in the original
        source string
      - err.message equals "Unrecognized character '<character>' at position <position>"
      measurement_source: instanceof checks and property reads on the caught error
        in Vitest tests
      durable_state: none
      dod_obligations:
      - id: dod-err-lex-01
        text: 'tokenize(''2 + $'').catch: err.character === ''$'' and err.position
          === 4'
      - id: dod-err-lex-02
        text: tokenize('2 ^ 3') throws LexError with err.character === '^'
      - id: dod-err-lex-03
        text: tokenize('sin(2)') throws LexError with err.character === 's', not a
          deferred ParseError from a later stage
      - id: dod-err-lex-04
        text: only the first unrecognized character is reported; tokenize does not
          return or collect a list of multiple LexError instances
    - id: op-error-parse-contract
      actor: consumer
      verb: catch
      target: ParseError thrown by parse(input) or by evaluate's internal parse call
      trigger: consumer calls parse or evaluate with lexically valid but grammatically
        malformed input
      preconditions:
      - input is lexically valid (tokenize would not throw) but violates a production
        in design:grammar-design section 2.1
      expected_outcomes:
      - thrown value is instanceof ParseError and instanceof ExprError
      - err.expected is a non-generic, site-specific description matching the grammar
        position that failed
      - no partial ExprNode is returned alongside or instead of the thrown error
      measurement_source: instanceof checks and property reads on the caught error
        in Vitest tests
      durable_state: none
      dod_obligations:
      - id: dod-err-parse-01
        text: parse('1 +') throws ParseError with err.expected === "a number, unary
          minus, or '(' after '+'"
      - id: dod-err-parse-02
        text: parse('(1 + 2') throws ParseError with err.expected === "')'"
      - id: dod-err-parse-03
        text: parse('1 2') throws ParseError with err.expected === 'end of input'
      - id: dod-err-parse-04
        text: the three err.expected values in dod-err-parse-01/02/03 are pairwise
          distinct strings
    - id: op-error-division-contract
      actor: consumer
      verb: catch
      target: DivisionByZeroError thrown by evaluate(input)
      trigger: consumer calls evaluate on an expression whose evaluation reaches a
        division node with a zero-valued right operand
      preconditions:
      - input is grammatically valid and its ExprNode contains at least one Binary('/')
        node
      - the right operand of that node evaluates to numeric 0
      expected_outcomes:
      - thrown value is instanceof DivisionByZeroError and instanceof ExprError, and
        is not instanceof ParseError
      - evaluate never returns Infinity, -Infinity, or NaN for a zero-divisor expression
      - the native '/' operator is never executed with a zero right-hand operand prior
        to the check
      measurement_source: instanceof checks and return-value inspection in Vitest
        tests
      durable_state: none
      dod_obligations:
      - id: dod-err-div-01
        text: evaluate('10 / 0') throws DivisionByZeroError, and (evaluate('10 / 0'))
          is never a returned numeric value
      - id: dod-err-div-02
        text: evaluate('0 / 0') throws DivisionByZeroError, not NaN
      - id: dod-err-div-03
        text: evaluate('10 / 0') does NOT throw ParseError or LexError
    - id: op-error-hierarchy-integrity
      actor: consumer
      verb: catch
      target: any error thrown across tokenize/parse/evaluate
      trigger: any invalid-input call to a public function, regardless of failure
        stage
      preconditions:
      - input triggers a lex, parse, or division-by-zero failure
      expected_outcomes:
      - the thrown value is instanceof ExprError in every case
      - the thrown value is instanceof exactly one of LexError, ParseError, DivisionByZeroError,
        never more than one, never zero
      - no thrown value is a direct instance of ExprError itself (ExprError is abstract
        and uninstantiable)
      forbidden_actors:
      - no module other than src/errors.ts declares a class extending the native Error
      - src/tokenizer.ts never throws ParseError or DivisionByZeroError
      - src/parser.ts never throws LexError as a newly-constructed instance (it may
        only propagate one already thrown by its internal tokenize call) and never
        throws DivisionByZeroError
      - src/evaluator.ts never throws LexError or ParseError as newly-constructed
        instances (it may only propagate ones already thrown by its internal parse/tokenize
        calls)
      measurement_source: instanceof checks in Vitest tests against caught errors,
        and static review of throw sites per module
      durable_state: none
      dod_obligations:
      - id: dod-err-hierarchy-01
        text: 'for each of tokenize(''2 $ 3''), parse(''1 +''), evaluate(''1 / 0''):
          the caught error is instanceof ExprError'
      - id: dod-err-hierarchy-02
        text: no test observes err.constructor.name === 'Error' for any invalid input
          across tokenize, parse, or evaluate
      - id: dod-err-hierarchy-03
        text: grep of src/tokenizer.ts, src/parser.ts, src/evaluator.ts contains no
          'extends Error' or 'class ExprError' declaration outside src/errors.ts
---

# docs/design/error_handling_design.md
