// @generated-by: codd implement
// @generated-from: docs/design/error_handling_design.md (design:error-handling-design)
// @design-node: docs/design/error_handling_design.md
// @output-paths: src, tests
// @generated-from: docs/governance/error_taxonomy_decision.md (governance:error-taxonomy-decision)
// @generated-from: docs/design/public_api_design.md (design:public-api-design)
// @generated-from: docs/requirements/requirements.md (req:exprcalc-requirements)

export abstract class ExprError extends Error {
  constructor(message: string) {
    super(message);
    this.name = new.target.name;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export class LexError extends ExprError {
  constructor(
    public readonly character: string,
    public readonly position: number,
  ) {
    super(`Unrecognized character '${character}' at position ${position}`);
  }
}

export class ParseError extends ExprError {
  constructor(public readonly expected: string) {
    super(`Parse error: expected ${expected}`);
  }
}

export class DivisionByZeroError extends ExprError {
  constructor() {
    super("Division by zero");
  }
}
