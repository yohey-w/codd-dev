# Claude Code x CoDD Setup Guide

Use this guide to wire CoDD into Claude Code so dependency scans and validation happen as part of your normal edit loop.

## Quick Start (5 Minutes)

1. Install CoDD.
2. Register the CoDD skill directory in Claude Code.
3. Add project-level hooks in `.claude/settings.json`.
4. Install a git `pre-commit` hook that runs `codd validate`.
5. Work through the standard CoDD loop: init -> scan -> impact -> fix -> validate.

## 1. Install CoDD

```bash
pip install codd-dev
```

Verify the CLI is available:

```bash
codd --help
```

## 2. Register CoDD Skills in Claude Code

If your Claude Code setup supports custom skill directories, add the CoDD skill pack to your user settings:

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "skillsPath": [
    "/mnt/c/tools/shogun-codd/skills"
  ]
}
```

This repo currently ships these starter skills:

- `codd-init`
- `codd-scan`
- `codd-impact`

For a fuller Claude Code command palette, add wrappers that expose the same CLI flow for:

- `codd-validate`
- `codd-generate`

## 3. Add Project Hooks in `.claude/settings.json`

Create `.claude/settings.json` in the project root so the whole team shares the same CoDD automation:

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/install-codd-pre-commit.sh"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "async": true,
            "command": "cd \"$CLAUDE_PROJECT_DIR\" && codd scan --path ."
          }
        ]
      }
    ]
  }
}
```

What this does:

- `SessionStart` keeps the repo's git `pre-commit` hook installed whenever Claude Code opens or resumes the project.
- `PostToolUse` re-runs `codd scan --path .` after file edits so `graph.db` stays fresh while the agent works.

## 4. Install the Git `pre-commit` Hook

Create `.claude/hooks/install-codd-pre-commit.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

HOOK_PATH="${CLAUDE_PROJECT_DIR}/.git/hooks/pre-commit"

cat > "$HOOK_PATH" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

echo "[CoDD] validating dependency graph before commit..."
codd validate --path .
EOF

chmod +x "$HOOK_PATH"
```

Make the installer executable:

```bash
chmod +x .claude/hooks/install-codd-pre-commit.sh
```

Why register it through `settings.json`:

- New contributors get the same validation hook without remembering a manual setup step.
- Updating the installer script updates the repo-wide `pre-commit` behavior on the next Claude Code session start.

## 5. End-to-End Workflow

Once Claude Code and CoDD are wired together, use this loop:

1. Open the project in Claude Code.
2. Run `codd init --project-name "my-project" --language "typescript"` if the repo is not initialized yet.
3. Write or import requirements and design docs under `docs/`.
4. Run `codd scan --path .` to build the initial dependency graph.
5. Make code or document changes. The `PostToolUse` hook keeps `graph.db` synchronized after edits.
6. Run `codd impact --diff HEAD~1 --path .` to see Green, Amber, and Gray impact bands.
7. Apply the required doc, code, and test updates based on the impact report.
8. Run `codd validate --path .` before commit if you want an explicit manual check.
9. Commit your work. The git `pre-commit` hook runs `codd validate --path .` automatically.

If you expose CoDD as Claude Code skills, the same loop maps cleanly to slash-style commands:

1. `/codd-init`
2. `/codd-scan`
3. Edit code and docs
4. `/codd-impact`
5. Fix affected docs, code, and tests
6. `/codd-validate`

## Recommended Skill Set for Claude Code

Use these skills or wrappers to cover the full CoDD lifecycle:

- `codd-init` for bootstrapping `codd/` and annotations
- `codd-scan` for rebuilding the graph after edits
- `codd-impact` for git-diff-based impact analysis
- `codd-validate` for pre-commit and CI checks
- `codd-generate` for Wave-order design document generation
