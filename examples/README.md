# CoDD Examples

Delivery vehicles for the greenfield autopilot ("write requirements, walk
away"). All three run the same stage sequence as `codd greenfield`; pick the
one that fits your environment. None of them assume which AI CLI sits
underneath — that is your project's `codd.yaml` `ai_command`.

| Vehicle | File | For whom |
| --- | --- | --- |
| **One command** (preferred) | `codd greenfield --requirements <file>` | Everyone. Checkpoint/resume (`.codd/greenfield_session.yaml`), `--dry-run`, `--ntfy-topic` notifications. |
| **Shell composition** | [`greenfield_autopilot.sh`](greenfield_autopilot.sh) | Users who want the pipeline as transparent, auditable shell — CI splicing, custom CLIs, stage-by-stage control. |
| **Claude Code Agent Workflow** | [`claude_workflows/codd-greenfield.js`](claude_workflows/codd-greenfield.js) | Claude Code users who want per-phase supervision. Copy into your project's `.claude/workflows/`. |
| **Skill (Claude + Codex)** | `codd skills install codd-greenfield --target both` | Conversational trigger ("build this from requirements") in Claude Code or Codex CLI. Source: [`../skills/codd-greenfield/`](../skills/codd-greenfield/). |

## greenfield_autopilot.sh

```bash
./greenfield_autopilot.sh my-app --language python \
    --requirements docs/requirements/requirements.md \
    [--ai-cmd 'your-ai-cli'] [--max-repair 10]
```

Composes the stage CLI directly: `init` → `elicit` (advisory) →
`plan --init --force` → `generate --all-waves --force` → per-task
`implement plan/steps/run` (tasks enumerated via
`implement list-tasks --format json`) → `verify --auto-repair --repair-mode
automatic` → `propagate` (advisory) → `codd check` as the final gate whose
exit code is the script's exit code. Requires `python3` or `jq`.

## claude_workflows/codd-greenfield.js

```text
cp claude_workflows/codd-greenfield.js  YOUR_PROJECT/.claude/workflows/
# inside Claude Code:
/workflows codd-greenfield docs/requirements/requirements.md
```

A thin supervisor: one workflow phase per pipeline stage, each phase spawns
an agent that runs the corresponding `codd` command via Bash. Stops at the
first failed phase and prints the resume command (`codd greenfield --resume`).
