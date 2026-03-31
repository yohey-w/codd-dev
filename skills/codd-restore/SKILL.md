# CoDD Restore

Reconstruct design documents from extracted code facts for brownfield projects. Unlike `/codd-generate` (which creates docs from requirements), `/codd-restore` asks "what IS the current design?" — reconstructing intent from code structure.

## When to Use

- After `codd extract` has generated extracted docs in `codd/extracted/`
- After `codd plan --init` has generated wave_config from extracted docs (or requirements)
- When you have an existing codebase with no design documentation
- When you want to infer requirements from code (wave 0 / requirements docs)

Do NOT use this for greenfield projects with requirements — use `/codd-generate` instead.

## Brownfield Flow

```
codd extract          # Step 1: Static analysis → codd/extracted/
codd plan --init      # Step 2: Extracted docs → wave_config (auto-detects brownfield)
codd restore --wave 0 # Step 3a: Infer requirements from code facts
codd restore --wave 2 # Step 3b: Reconstruct system design
codd restore --wave 3 # Step 3c: Reconstruct detailed design
codd scan --path .    # Step 4: Build dependency graph
```

## Requirements Inference (Wave 0)

When restoring a document under `docs/requirements/`, the restore command switches to **requirements inference mode**:

- Infers functional requirements from modules, classes, API routes, and function signatures
- Infers non-functional requirements from code patterns (async = performance, rate limiting = scalability, RLS = security)
- Infers constraints from frameworks, libraries, and architectural patterns
- Marks non-obvious inferences with `[inferred]` so humans can verify

**Important limitations** (the prompt explicitly warns the AI about these):

- Cannot know features that were planned but never implemented
- Cannot distinguish bugs from intentional behavior
- Cannot know business context not reflected in code

These are **inferred requirements** — describing what was built, not original intent. Human review is essential.

## Prerequisite Checks

Before every restore run, verify:

1. `codd/codd.yaml` exists with `wave_config`.
2. `codd/extracted/` contains extracted docs (run `codd extract` first if missing).
3. For Wave 2+, verify that earlier waves have been restored or exist already.
4. Confirm you are in the intended project root.

If any prerequisite fails, stop and resolve it.

## Recommended Workflow

Follow this loop for each wave:

1. Restore the target wave:
   ```bash
   codd restore --wave 0 --path .
   ```
   Replace `0` with the current wave number.

2. Read every restored document and confirm:
   - the content accurately describes the existing codebase
   - inferred requirements are reasonable and marked with `[inferred]`
   - no hallucinated capabilities were introduced
   - the document body starts directly with content, not AI meta commentary

3. Validate frontmatter and dependency references:
   ```bash
   codd validate --path .
   ```

4. Refresh the dependency graph:
   ```bash
   codd scan --path .
   ```

5. Pause for HITL confirmation before the next wave:
   - After requirements inference: `推定要件を確認しました。Wave 2（システム設計の復元）に進みますか？`
   - After any later wave: `Wave Nの設計書を復元しました。Wave N+1 に進みますか？`

## HITL Gates

Human review is mandatory between waves. Restored documents describe "what IS" but may misinterpret intent. Do not proceed automatically.

- Review goal 1: confirm restored content matches the actual codebase behavior
- Review goal 2: flag any inferred requirements that are actually bugs or unintended behavior
- Review goal 3: add business context the AI could not infer from code

If the human rejects a wave, revise with `--force`, re-validate, and request approval again.

## Greenfield vs Brownfield Decision

| Situation | Command |
|-----------|---------|
| Have requirements, no code | `/codd-generate` |
| Have code, no requirements | `/codd-restore` |
| Have both | `/codd-generate` (requirements take precedence) |
| Want to infer requirements from code | `codd restore --wave 0` (requirements inference) |

## Troubleshooting

- `Error: no extracted documents found`
  - Run `codd extract` first to generate extracted docs from source code.
- `Error: wave_config has no entries for wave N`
  - Run `codd plan --init` to generate wave_config from extracted docs.
- Restored content hallucinated capabilities not in the code
  - Use `--force` to regenerate, or manually edit the restored document.
- Inferred requirements too vague
  - Check that `codd extract` captured enough detail. Re-run extract with more source dirs if needed.

## Guardrails

- Use the `codd` command, not `python -m codd.cli`.
- Restore one wave at a time.
- Do not skip human approval gates — restored docs need human verification of intent.
- Do not advance to the next wave while validation is failing.
- Do not use restore for greenfield projects with existing requirements.
