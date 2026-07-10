// @generated-by: codd implement
// @generated-from: docs/detailed_design/parser_ast_flow.md (detailed_design:parser-ast-flow)
// @design-node: docs/detailed_design/parser_ast_flow.md
// @output-paths: src, tests

export type ExprNode = NumberNode | UnaryNode | BinaryNode;

export interface NumberNode {
  readonly type: "Number";
  readonly value: number;
}

export interface UnaryNode {
  readonly type: "Unary";
  readonly operand: ExprNode;
}

export interface BinaryNode {
  readonly type: "Binary";
  readonly operator: "+" | "-" | "*" | "/";
  readonly left: ExprNode;
  readonly right: ExprNode;
}

export function makeNumberNode(value: number): NumberNode {
  return Object.freeze({ type: "Number", value });
}

export function makeUnaryNode(operand: ExprNode): UnaryNode {
  return Object.freeze({ type: "Unary", operand });
}

export function makeBinaryNode(
  operator: "+" | "-" | "*" | "/",
  left: ExprNode,
  right: ExprNode,
): BinaryNode {
  return Object.freeze({ type: "Binary", operator, left, right });
}
