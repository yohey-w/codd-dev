# CoDD Assemble

Assemble generated sprint fragments into a complete, buildable project. This is the final step in the greenfield pipeline after `codd implement`.

## Usage

Use this skill after all sprints have been generated via `codd implement`. The assembler reads design documents and generated code fragments, then invokes AI to produce a unified project with all configuration files, entry points, and source code.

## Prerequisites

1. `codd implement` has been run for all sprints.
2. Generated fragments exist in `src/generated/sprint_N/` directories.
3. Design documents exist in `docs/` with valid frontmatter.

## Workflow

```bash
# Basic usage — assembles into src/
codd assemble --path .

# Custom output directory
codd assemble --path . --output-dir app

# Override AI command (e.g., use a different model)
codd assemble --path . --ai-cmd 'claude --print --model claude-opus-4-6 --tools ""'
```

## What It Does

1. Collects all design documents (architecture, component design, state management, etc.)
2. Collects all generated code fragments from `src/generated/sprint_N/`
3. Builds a prompt with both design context and code fragments
4. Invokes AI to produce a unified project:
   - **Project configuration**: package.json, tsconfig.json, next.config.*, tailwind.config.*, etc.
   - **Entry points**: app/layout.tsx, app/page.tsx, globals.css, etc.
   - **Source code**: components, utilities, types, hooks, reducers
5. Parses `=== FILE: path ===` blocks from AI output and writes files

## Output

Files are written relative to the project root. The assembler produces both:
- Configuration files at the project root (package.json, tsconfig.json, etc.)
- Source files under the output directory (default: `src/`)

## HITL Gate

After assembly, verify the project builds:

```bash
# For Node.js/TypeScript projects
npm install
npm run build

# For Python projects
python -m pytest
```

If the build fails, check:
- Missing imports between assembled files
- Configuration mismatches (TypeScript strict mode, module resolution)
- Framework-specific entry point conventions

## Integration in Full Pipeline

```bash
#!/bin/bash
set -e

codd init --requirements spec.md
codd plan --init

waves=$(codd plan --waves)
for wave in $(seq 1 $waves); do
  codd generate --wave $wave
done

codd validate

sprints=$(codd plan --sprints)
for sprint in $(seq 1 $sprints); do
  codd implement --sprint $sprint
done

# Final step: assemble fragments into a working project
codd assemble
```

## Troubleshooting

- `No generated fragments found in src/generated/`
  - Run `codd implement` first. Fragments are created per sprint.
- Build fails after assembly
  - Check that the design documents specify the correct framework and configuration.
  - Re-run with `--ai-cmd` pointing to a more capable model (e.g., Opus) for better integration.
- Missing configuration files (package.json, tsconfig.json)
  - The assembler prompt explicitly requests these. If missing, re-run assembly.

## Guardrails

- Always verify the build after assembly.
- Do not manually edit assembled files before verifying the build — establish a clean baseline first.
- If the assembled output is unsatisfactory, fix the design documents or implementation plan and regenerate, rather than patching assembled code.
