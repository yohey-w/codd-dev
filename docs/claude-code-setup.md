# Claude Code x CoDD Setup Guide

Wire CoDD into Claude Code so dependency scans and validation happen as part of your normal edit loop. Once set up, **you never think about graph maintenance again** — hooks handle it automatically.

## Quick Start (5 Minutes)

1. Install CoDD
2. Register CoDD Skills in Claude Code
3. Add project-level hooks in `.claude/settings.json`
4. Install a git `pre-commit` hook
5. Work through the standard CoDD loop: init → generate → scan → impact

## 1. Install CoDD

```bash
pip install codd-dev
```

Verify the CLI is available:

```bash
codd --help
```

## 2. Register CoDD Skills in Claude Code

Add the CoDD skill directory to your Claude Code settings (`~/.claude/settings.json` or `.claude/settings.json`):

```json
{
  "skillsPath": [
    "<path-to-codd-dev>/skills"
  ]
}
```

This registers all CoDD Skills as slash commands:

| Skill | What it does |
|-------|-------------|
| `/codd-init` | Initialize project + import requirements |
| `/codd-generate` | Generate design docs wave-by-wave with HITL gates (greenfield) |
| `/codd-restore` | Reconstruct design docs from extracted code facts (brownfield) |
| `/codd-scan` | Rebuild dependency graph from frontmatter |
| `/codd-impact` | Change impact analysis with Green/Amber/Gray protocol |
| `/codd-validate` | Frontmatter & dependency consistency check |

## 3. Add Project Hooks in `.claude/settings.json`

Create `.claude/settings.json` in the project root so the whole team shares the same CoDD automation:

```json
{
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

- **SessionStart**: Keeps the git `pre-commit` hook installed whenever Claude Code opens or resumes the project.
- **PostToolUse**: Re-runs `codd scan` after every file edit. **The dependency graph is always current — you never run `codd scan` manually again.**

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

- New contributors get the same validation hook without a manual setup step.
- Updating the installer script updates the repo-wide `pre-commit` behavior on next session start.

## 5. End-to-End Workflow

### CLI Workflow

```bash
# 1. Initialize with requirements (any format: txt, md, doc)
codd init --project-name "my-project" --language "typescript" \
  --requirements spec.txt

# 2. AI generates design docs (wave_config auto-generated)
codd generate --wave 2

# 3. Build dependency graph
codd scan --path .

# 4. Edit requirements or design docs...

# 5. See what's affected (detects uncommitted changes)
codd impact --path .

# 6. Validate before commit (also runs automatically via pre-commit hook)
codd validate --path .
```

### Skills Workflow (Recommended)

With Skills, Claude handles the flags and adds HITL gates automatically:

```
You:  /codd-init
      → Claude runs init with --requirements, adds frontmatter

You:  /codd-generate
      → Claude generates Wave 2, reviews output, asks approval
      → "Wave 2 design docs reviewed. Proceed to Wave 3?"

You:  yes

You:  (edit requirements — add a new feature)

You:  /codd-impact
      → Claude detects changes, follows Green/Amber/Gray protocol
      → Green Band: auto-updates safe docs
      → Amber Band: "test-strategy is affected. Update it?"
```

### What You Actually Do Day-to-Day

With hooks active, your daily workflow is:

1. **Edit files normally.** The PostToolUse hook runs `codd scan` after every edit — invisible.
2. **Run `/codd-impact` when you want to know what's affected.** The graph is always current.
3. **Commit.** The pre-commit hook runs `codd validate` — broken coherence can't be committed.

That's it. Graph maintenance is completely invisible. You focus on building; CoDD focuses on coherence.
