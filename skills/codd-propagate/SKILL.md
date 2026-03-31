# CoDD Propagate

Reverse-propagate source code changes to design documents. When source code changes, `codd propagate` identifies which design docs are affected via the `modules` field and optionally updates them using AI.

## When to Use

- After modifying source code and before committing
- When you want to check which design docs are affected by code changes
- When you want to auto-update design docs to reflect code changes
- As part of the modification flow (code changed → propagate → review → commit)

Do NOT use this for:

- Document-to-document propagation — use `/codd-impact` instead
- Initial design doc generation — use `/codd-generate` (greenfield) or `/codd-restore` (brownfield)

## Propagation Direction

CoDD supports two propagation directions:

| Direction | Command | Trigger | Mechanism |
|-----------|---------|---------|-----------|
| Forward (doc → doc) | `codd impact` | Design doc changed | CEG dependency graph (`depends_on`) |
| Reverse (code → doc) | `codd propagate` | Source code changed | `modules` field in frontmatter |

`/codd-propagate` handles the **reverse** direction only.

## How It Works

```
git diff HEAD              # 1. Detect changed source files
  ↓
source_dirs mapping        # 2. Map files to modules (e.g. src/auth/service.py → "auth")
  ↓
modules field lookup       # 3. Find design docs with matching modules
  ↓
Analysis report            # 4. Show which docs need attention
  ↓ (--update only)
AI update                  # 5. AI updates affected doc bodies
```

## Prerequisite Checks

Before running propagate, verify:

1. `codd/codd.yaml` exists with `source_dirs` configured in `scan`.
2. Design docs have `modules` field in their frontmatter.
3. There are uncommitted source code changes (or specify `--diff` target).
4. Confirm you are in the intended project root.

If `modules` fields are missing, run `codd plan --init` to regenerate wave_config with modules, then regenerate or restore design docs.

## Recommended Workflow

### Analysis Only (Default)

```bash
codd propagate --path .
```

This shows:

- Changed source files and their module mappings
- Design docs affected by those changes
- Suggestion to run with `--update` if updates are needed

Review the analysis before deciding whether to update.

### With AI Update

```bash
codd propagate --path . --update
```

This additionally:

- Calls AI to update each affected design doc body
- Preserves frontmatter exactly as-is
- Leaves doc body unchanged for bug fixes or refactoring that don't affect design

After update, review every changed document before committing.

## HITL Gates

1. **After analysis**: Review the file → module → doc mapping. Confirm the affected docs list is correct before running with `--update`.
2. **After update**: Read every updated design doc. Confirm:
   - Changes accurately reflect the code modification
   - No unrelated sections were altered
   - Bug fixes / refactoring correctly left the doc body unchanged
   - Frontmatter was preserved exactly

## When NOT to Update

Not every code change needs a design doc update. The AI prompt explicitly tells the model to leave the body unchanged for:

- Bug fixes that don't change the design
- Minor refactoring (rename, extract method)
- Performance optimizations that don't change architecture
- Test additions or modifications

Use analysis-only mode (`codd propagate` without `--update`) to check which docs are affected, then decide manually whether an update is warranted.

## Options

| Flag | Description |
|------|-------------|
| `--path` | Project root (default: current directory) |
| `--diff` | Git diff target (default: `HEAD`) |
| `--update` | Actually update affected docs via AI (default: analysis only) |
| `--ai-cmd` | Override AI command for this run |

## Complete Modification Flow

After modifying source code:

```bash
# 1. Check reverse impact (code → docs)
codd propagate --path .

# 2. If design docs need updating
codd propagate --path . --update

# 3. Rebuild dependency graph
codd scan --path .

# 4. Check forward impact (doc → doc) if design docs changed
codd impact --path .

# 5. Validate and commit
codd validate --path .
git add -A && git commit
```

## Troubleshooting

- `No changed files detected`
  - Ensure you have uncommitted changes, or specify a different `--diff` target (e.g. `--diff HEAD~3`).
- `No affected design docs found`
  - Check that `source_dirs` is configured in `codd.yaml` → `scan`.
  - Check that design docs have `modules` fields in their frontmatter.
  - Verify the module names match (e.g. `src/auth/service.py` maps to module `auth`).
- Updated doc body is empty or garbled
  - Re-run with `--update`. If the AI output is still bad, manually edit the doc.

## Guardrails

- Use the `codd` command, not `python -m codd.cli`.
- Always run analysis first (without `--update`) to confirm affected docs before updating.
- Do not skip human review of updated documents.
- Do not combine with `codd impact` in the same step — propagate handles code→doc, impact handles doc→doc.
- After propagate with `--update`, always run `codd scan` to refresh the dependency graph.
