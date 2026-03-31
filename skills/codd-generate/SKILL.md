# CoDD Generate

Generate CoDD design documents one wave at a time, validate their frontmatter, refresh the dependency graph, and stop for human approval before advancing to the next wave.

**This is for greenfield projects** (requirements → design). For brownfield projects (existing code → design), use `/codd-restore` instead.

## Usage

Use this skill after `codd init` and after requirement documents are ready. Generate only one wave at a time. Never auto-run the next wave without a human decision.

If you have an existing codebase with no requirements, use the brownfield flow instead:
1. `codd extract` — reverse-engineer code structure
2. `codd plan --init` — generate wave_config from extracted docs
3. `/codd-restore` — reconstruct design docs from extracted facts

## Wave Model

CoDD design generation follows dependency order. Documents in the same wave may be generated together, but later waves must wait until the previous wave is validated and reviewed.

- Wave 1: requirement-only artifacts such as acceptance criteria and decisions
- Wave 2: system-level design derived from requirements plus Wave 1 outputs
- Wave 3: detailed design artifacts derived from approved system design
- Wave 4+: later artifacts only after the previous wave is validated and approved

## Auto Wave Config

Since v0.2.0a4, `codd generate` automatically generates `wave_config` from requirement documents if it's missing from `codd.yaml`. You no longer need to run `codd plan --init` manually before generating. The flow is:

1. Write requirements in any format (plain text, markdown, etc.) and import with `codd init --requirements <file>` — CoDD adds frontmatter automatically
2. Run `codd generate --wave 2` — wave_config is auto-generated from requirements
3. Design docs are generated in the correct dependency order

This means **the only human input is the requirements**. Everything else — wave_config, frontmatter, dependency declarations — is derived automatically.

## Prerequisite Checks

Before every generation run, verify these conditions:

1. `codd/codd.yaml` exists in the project root.
2. At least one requirement document exists under configured `doc_dirs`. If imported with `codd init --requirements`, frontmatter is already added. Otherwise, ensure `node_id` and `type: requirement` are present in frontmatter.
3. For Wave 1, `codd init` has been completed successfully.
4. For Wave 2 and later, every output from the previous wave exists and `codd validate --path .` passes.
5. Confirm you are in the intended project root before running generation commands.

If any prerequisite fails, stop and resolve it before generating.

## Recommended Workflow

Follow this loop for each wave:

1. Generate the target wave:
   ```bash
   codd generate --wave 1 --path .
   ```
   Replace `1` with the current wave number.

2. Read every generated document and confirm:
   - the content matches the requirement scope
   - no unsupported features were invented
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
   - After Wave 1: `Wave 1の設計書を確認しました。Wave 2に進みますか？`
   - After any later wave: `Wave Nの成果物を確認しました。Wave N+1 に進みますか？`

This skill covers the full `generate -> scan -> validate` checkpoint set, while the safer operator order is `generate -> read -> validate -> scan -> HITL`.

## HITL Gates

Human review is mandatory between waves. Do not proceed automatically.

- Review goal 1: confirm the generated artifacts stay within requirement scope
- Review goal 2: catch wrong architectural direction before it propagates downstream
- Review goal 3: approve the next wave explicitly

If the human rejects the wave, revise the current wave first, re-run validation, re-run scan, and request approval again.

## Frontmatter Validation Checklist

Run `codd validate --path .` after each generation and verify:

- every generated document has CoDD frontmatter
- `node_id` and `type` are present
- `depends_on` references resolve to existing upstream nodes
- wave ordering is consistent with dependency declarations
- validation exits successfully before you continue

If validation fails, fix the generated document or the wave configuration before moving forward.

## Suggested Commands by Wave

### Wave 1

```bash
codd generate --wave 1 --path .
codd validate --path .
codd scan --path .
```

Preconditions:
- `codd init` has been run
- `codd/codd.yaml` exists
- requirement documents are present

### Wave 2+

```bash
codd generate --wave 2 --path .
codd validate --path .
codd scan --path .
```

Preconditions:
- previous wave outputs exist
- previous wave passed `codd validate --path .`
- human approved progression to the next wave

## Troubleshooting

- `Error: codd/ not found. Run 'codd init' first.`
  - Run `codd init`, create `codd/codd.yaml`, and retry.
- Validation errors after generation
  - Read the reported file, correct frontmatter or dependency references, then run `codd validate --path .` again.
- Generated content drifts beyond requirements
  - discard the unsupported direction, regenerate the same wave with tighter context, and repeat review before advancing.
- Dependency graph looks stale
  - re-run `codd scan --path .` after validation completes.

## Guardrails

- Use the `codd` command, not `python -m codd.cli`.
- Generate one wave at a time.
- Do not skip human approval gates.
- Do not advance to the next wave while validation is failing.
