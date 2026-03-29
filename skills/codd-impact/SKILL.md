# CoDD Impact Analysis

Use this skill after a requirements, design, code, or test change to determine what CoDD artifacts must be updated next. The goal is not only to read the impact report, but to decide whether the AI should update design documents immediately, ask a human for approval, or only report informational findings. Treat the impact report as an action queue for keeping requirements, design, implementation, and tests coherent.

## Primary Command

Run impact analysis from the project root:

```bash
codd impact --path .
```

By default, this detects **uncommitted changes** (compares working tree to HEAD). No need to commit first.

If you need a report file:

```bash
codd impact --path . --output "codd/reports/impact_$(date +%Y%m%d_%H%M%S).md"
```

If you need to compare against a specific commit instead of uncommitted changes:

```bash
codd impact --diff <commit-hash> --path .
```

## Preflight

1. Confirm you are at the project root and `codd/codd.yaml` exists.
2. Confirm `codd/scan/` directory exists (contains nodes.jsonl and edges.jsonl).
3. If `codd/scan/` is missing or empty, run:

```bash
codd scan --path .
```

4. Run `codd impact --path .`.
5. Read the report in this order:
   - Convention Alerts
   - Green Band
   - Amber Band
   - Gray Band

## How To Act On Each Band

### Convention Alerts

Convention Alerts have higher priority than Green, Amber, or Gray items. They mean an implicit rule may have been violated.

When a Convention Alert appears:

1. Open `codd/annotations/conventions.yaml`.
2. Find the rule that matches the reported target and reason.
3. Identify which invariant is being protected and which changed artifact triggered it.
4. Update the affected design docs, tests, or governance docs so the invariant is either preserved or explicitly re-decided.
5. If the business rule itself changed, do not silently override it. Ask the human to confirm the convention change before editing `conventions.yaml`.

## Green Band

Green Band means the impact is high-confidence and the AI may update affected design documents autonomously.

Action:

1. Open each affected design document reported in Green Band.
2. Read the changed source artifact and the impacted design document together.
3. Update the impacted document's frontmatter if dependencies, conventions, or data dependencies changed.
4. Update the body to reflect the new behavior, interface, data flow, constraints, acceptance criteria, or verification approach.
5. Save the document.
6. Run `codd scan --path .` to rebuild the graph after the document update.
7. Re-run `codd impact --path .` to confirm the remaining impact set is reduced or reclassified.

Use Green Band for autonomous propagation. Do not wait for human approval unless the change introduces a product decision that is not already justified in the source requirements or governance docs.

## Amber Band

Amber Band means the impact is plausible but requires human confirmation before the AI edits the affected document.

Ask the human with this template:

`{設計書名}が影響を受けています。更新しますか？（変更内容: {変更サマリ}）`

Action:

1. Summarize why the document is impacted.
2. Quote the document name and the change summary in the template above.
3. Wait for confirmation before editing the document.
4. If approved, update the document, run `codd scan --path .`, then re-run `codd impact --path .`.

## Gray Band

Gray Band is informational only. Report it, but do not edit anything unless the human explicitly asks you to.

Use this message template:

`{設計書名}への間接的な影響が検出されましたが、対応不要と判断します`

Gray items should still be mentioned in the final status so the human knows they were reviewed.

## Design Document Update Procedure

When Green Band allows autonomous updates, or Amber Band gets approved, update affected design documents with this procedure:

1. Open the impacted document and the changed upstream artifact side by side.
2. Identify exactly what changed:
   - requirement or scope
   - interface or API contract
   - data model or data flow
   - operational rule
   - test expectation
3. Update frontmatter fields first when needed:
   - `depends_on`
   - `conventions`
   - `data_dependencies`
   - node metadata that became stale
4. Update the human-readable body second:
   - summary or purpose
   - flows, diagrams, steps, contracts, or constraints
   - acceptance criteria or verification notes affected by the change
5. Preserve traceability. Do not add behavior that is not justified by requirements, governance, or the changed source artifact.
6. Save the document.
7. Run `codd scan --path .`.
8. Run `codd impact --path .` again.
9. Report what changed, what was intentionally left unchanged, and whether any Amber or Gray items remain.

## Reporting Rules

When presenting the result, organize it as:

1. Convention Alerts requiring attention
2. Green Band documents updated automatically
3. Amber Band documents awaiting or receiving human approval
4. Gray Band informational items with no action taken

Always name the exact design documents you changed. If nothing needed an edit, say so explicitly.

## Troubleshooting

### Graph data not found

The dependency graph has not been built yet or was deleted. Run:

```bash
codd scan --path .
```

Then run:

```bash
codd impact --path .
```

### `No changes detected`

Your diff target may not match the change you intend to analyze. Re-run impact with an explicit commit hash:

```bash
codd impact --diff <commit-hash> --path .
```

Choose the commit by checking recent history with `git log --oneline`.
