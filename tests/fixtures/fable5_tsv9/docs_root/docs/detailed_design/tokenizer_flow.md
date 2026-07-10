---
codd:
  node_id: detailed_design:tokenizer-flow
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
    - module:tokenizer
    reason: Whitespace must be ignored and unrecognized characters must raise LexError
      with character and position; this is the release-blocking lexing contract.
  modules:
  - tokenizer
  - errors
  operation_flow:
    operations:
    - id: op-tokenizer-scan-success
      actor: consumer
      verb: call
      target: tokenize(text) returning Token[]
      trigger: consumer calls tokenize with text containing only digits, '.', '+',
        '-', '*', '/', '(', ')', and whitespace
      preconditions:
      - every character in text maps to a TokenKind or is whitespace
      expected_outcomes:
      - tokenize returns an array of Token objects, one per non-whitespace lexical
        unit, in left-to-right source order
      - no returned Token has kind corresponding to a whitespace character
      - each NUMBER token's text is the maximal contiguous run of digits (and optional
        '.') starting at its position
      measurement_source: return-value inspection in Vitest tests (tests/tokenizer.test.ts)
      durable_state: none
      dod_obligations:
      - id: dod-tok-scan-01
        text: tokenize('1   +   2') returns tokens with kinds [NUMBER, PLUS, NUMBER]
          and no WHITESPACE-kind token exists
      - id: dod-tok-scan-02
        text: tokenize('12') returns a single NUMBER token with text === '12', not
          two NUMBER tokens ['1','2']
      - id: dod-tok-scan-03
        text: tokenize('') returns an empty Token[] and does not throw
    - id: op-tokenizer-lex-error
      actor: consumer
      verb: catch
      target: LexError thrown by tokenize(text)
      trigger: consumer calls tokenize with text containing a character not mapped
        to number, +, -, *, /, (, ) or whitespace
      preconditions:
      - the scan reaches the Ready state with a character matching no outgoing edge
        except LexErrorState
      expected_outcomes:
      - thrown value is instanceof LexError and instanceof ExprError
      - err.character equals the exact offending character at the scanner's cursor
      - err.position equals the scanner's cursor value at the moment of failure, unaffected
        by prior whitespace-skip transitions
      - the scan loop performs no further transitions after entering LexErrorState
      measurement_source: instanceof checks and property reads on the caught error
        in Vitest tests (tests/tokenizer.test.ts)
      durable_state: none
      dod_obligations:
      - id: dod-tok-lex-01
        text: tokenize('2 + $') throws LexError with character === '$' and position
          === 4
      - id: dod-tok-lex-02
        text: tokenize(' $') throws LexError with position === 1, confirming leading
          whitespace shifts position correctly
      - id: dod-tok-lex-03
        text: tokenize('sin(2)') throws LexError with character === 's' and position
          === 0, before any NUMBER or LPAREN token is emitted
---

# docs/detailed_design/tokenizer_flow.md
