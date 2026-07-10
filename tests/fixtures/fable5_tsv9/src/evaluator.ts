// @generated-by: codd implement
// @generated-from: docs/detailed_design/evaluator_flow.md (detailed_design:evaluator-flow)
// @design-node: docs/detailed_design/evaluator_flow.md
// @output-paths: src, tests
// @generated-from: docs/design/error_handling_design.md (design:error-handling-design)

import type { ExprNode } from "./ast.js";
import { parse } from "./parser.js";
import { DivisionByZeroError } from "./errors.js";

function evalNode(node: ExprNode): number {
  switch (node.type) {
    case "Number":
      return node.value;
    case "Unary":
      return -evalNode(node.operand);
    case "Binary": {
      const left = evalNode(node.left);
      const right = evalNode(node.right);
      switch (node.operator) {
        case "+":
          return left + right;
        case "-":
          return left - right;
        case "*":
          return left * right;
        case "/":
          if (right === 0) {
            throw new DivisionByZeroError();
          }
          return left / right;
        default: {
          const exhaustive: never = node.operator;
          throw new Error(`unreachable operator: ${exhaustive as string}`);
        }
      }
    }
    default: {
      const exhaustive: never = node;
      throw new Error(`unreachable node type: ${(exhaustive as ExprNode).type}`);
    }
  }
}

export function evaluate(input: ExprNode | string): number {
  const node = typeof input === "string" ? parse(input) : input;
  return evalNode(node);
}
