// @generated-by: codd implement
// @generated-from: docs/detailed_design/tokenizer_flow.md (detailed_design:tokenizer-flow)
// @design-node: docs/detailed_design/tokenizer_flow.md
// @output-paths: src, tests
// @generated-from: docs/design/error_handling_design.md (design:error-handling-design)

import { LexError } from "./errors.js";

export type TokenKind = "number" | "operator" | "lparen" | "rparen";

export interface Token {
  readonly kind: TokenKind;
  readonly value: string;
  readonly position: number;
}

const WHITESPACE_PATTERN = /\s/;
const DIGIT_PATTERN = /[0-9]/;
const NUMBER_CONTINUATION_PATTERN = /[0-9.]/;
const OPERATOR_CHARS = new Set(["+", "-", "*", "/"]);

export function tokenize(text: string): Token[] {
  const tokens: Token[] = [];
  let cursor = 0;

  while (cursor < text.length) {
    const char = text[cursor];

    if (WHITESPACE_PATTERN.test(char)) {
      cursor++;
      continue;
    }

    if (DIGIT_PATTERN.test(char) || char === ".") {
      const start = cursor;
      let buffer = "";
      while (cursor < text.length && NUMBER_CONTINUATION_PATTERN.test(text[cursor])) {
        buffer += text[cursor];
        cursor++;
      }
      tokens.push({ kind: "number", value: buffer, position: start });
      continue;
    }

    if (OPERATOR_CHARS.has(char)) {
      tokens.push({ kind: "operator", value: char, position: cursor });
      cursor++;
      continue;
    }

    if (char === "(") {
      tokens.push({ kind: "lparen", value: char, position: cursor });
      cursor++;
      continue;
    }

    if (char === ")") {
      tokens.push({ kind: "rparen", value: char, position: cursor });
      cursor++;
      continue;
    }

    throw new LexError(char, cursor);
  }

  return tokens;
}
