# Repairability Classification

Classify each violation into exactly one group.

- **repairable**: A machine-generated patch can reasonably fix the issue.
- **pre_existing**: The issue predates the given baseline and should be reported separately.
- **unrepairable**: The issue needs human judgment before a patch is attempted.

## Rules

- If the relevant artifacts only come from history before `baseline_ref`, choose `pre_existing`.
- If the stated expectation and current artifacts have a broad mismatch, choose `unrepairable`.
- Otherwise, choose `repairable`.

## Input

violations: {{violations_json}}
baseline_ref: {{baseline_ref}}

## Output

Return only JSON in this shape:

{"<violation_id>": "repairable" | "pre_existing" | "unrepairable"}
