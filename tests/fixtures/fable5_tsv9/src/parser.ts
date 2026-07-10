// @generated-by: codd implement
// @generated-from: docs/detailed_design/parser_ast_flow.md (detailed_design:parser-ast-flow)
// @design-node: docs/detailed_design/parser_ast_flow.md
// @output-paths: src, tests
// @generated-from: docs/design/error_handling_design.md (design:error-handling-design)
// @generated-from: docs/design/grammar_design.md (design:grammar-design)
// @generated-from: docs/design/public_api_design.md (design:public-api-design)
// @generated-from: docs/design/system_design.md (design:system-design)
// @generated-from: docs/governance/error_taxonomy_decision.md (governance:error-taxonomy-decision)
// @generated-from: docs/requirements/requirements.md (req:exprcalc-requirements)
// @generated-from: docs/test/acceptance_criteria.md (test:acceptance-criteria)

import type { Token } from "./tokenizer.js";
import { tokenize } from "./tokenizer.js";
import type { ExprNode } from "./ast.js";
import { makeNumberNode, makeUnaryNode, makeBinaryNode } from "./ast.js";
import { ParseError } from "./errors.js";

type ParseResult = [ExprNode, number];

function isAdditiveOperator(token: Token | undefined): boolean {
  return (
    token !== undefined &&
    token.kind === "operator" &&
    (token.value === "+" || token.value === "-")
  );
}

function isMultiplicativeOperator(token: Token | undefined): boolean {
  return (
    token !== undefined &&
    token.kind === "operator" &&
    (token.value === "*" || token.value === "/")
  );
}

function isUnaryMinus(token: Token | undefined): boolean {
  return token !== undefined && token.kind === "operator" && token.value === "-";
}

function hasOperandStart(tokens: Token[], pos: number): boolean {
  const token = tokens[pos];
  return (
    token !== undefined &&
    (token.kind === "number" || token.kind === "lparen" || isUnaryMinus(token))
  );
}

function parseExpression(tokens: Token[], pos: number): ParseResult {
  let [left, next] = parseTerm(tokens, pos);
  while (isAdditiveOperator(tokens[next])) {
    const operator = tokens[next].value as "+" | "-";
    if (!hasOperandStart(tokens, next + 1)) {
      throw new ParseError(`a number, unary minus, or '(' after '${operator}'`);
    }
    const [right, after] = parseTerm(tokens, next + 1);
    left = makeBinaryNode(operator, left, right);
    next = after;
  }
  return [left, next];
}

function parseTerm(tokens: Token[], pos: number): ParseResult {
  let [left, next] = parseUnary(tokens, pos);
  while (isMultiplicativeOperator(tokens[next])) {
    const operator = tokens[next].value as "*" | "/";
    if (!hasOperandStart(tokens, next + 1)) {
      throw new ParseError(`a number, unary minus, or '(' after '${operator}'`);
    }
    const [right, after] = parseUnary(tokens, next + 1);
    left = makeBinaryNode(operator, left, right);
    next = after;
  }
  return [left, next];
}

function parseUnary(tokens: Token[], pos: number): ParseResult {
  const token = tokens[pos];
  if (isUnaryMinus(token)) {
    if (!hasOperandStart(tokens, pos + 1)) {
      throw new ParseError("a number, unary minus, or '(' after unary '-'");
    }
    const [operand, next] = parseUnary(tokens, pos + 1);
    return [makeUnaryNode(operand), next];
  }
  return parsePrimary(tokens, pos);
}

function parsePrimary(tokens: Token[], pos: number): ParseResult {
  const token = tokens[pos];

  if (token !== undefined && token.kind === "number") {
    return [makeNumberNode(Number(token.value)), pos + 1];
  }

  if (token !== undefined && token.kind === "lparen") {
    const [inner, next] = parseExpression(tokens, pos + 1);
    const closing = tokens[next];
    if (closing === undefined || closing.kind !== "rparen") {
      throw new ParseError("')'");
    }
    return [inner, next + 1];
  }

  throw new ParseError("a number, unary minus, or '('");
}

export function parse(input: string | Token[]): ExprNode {
  const tokens = typeof input === "string" ? tokenize(input) : input;
  const [node, next] = parseExpression(tokens, 0);
  if (next < tokens.length) {
    throw new ParseError("end of input");
  }
  return node;
}
