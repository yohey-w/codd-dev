---
name: codd-greenfield
description: |
  Run the CoDD greenfield autopilot: a requirements document in, a working system out, unattended. Use when the user wants to build a NEW system from a requirements doc ("greenfield", "build from requirements", "要件定義から自動構築", "write requirements and walk away") or to resume/inspect an interrupted autopilot run. Greenfield generation, NOT brownfield modification (use codd-evolve), NOT pure bug fix (use codd fix).
---

# CoDD Greenfield — Unattended Autopilot

Take a requirements document and build the entire system: init → elicit →
plan → generate → implement → verify (auto-repair) → propagate → check, with
every gate auto-approved. The user writes requirements and walks away; this
skill drives `codd greenfield` and watches the result.

Works identically whether this skill is installed for Claude Code
(`~/.claude/skills/`) or Codex CLI (`~/.agents/skills/`): the skill only runs
`codd` commands, and the AI CLI that CoDD itself invokes per stage comes from
the project's `codd.yaml` `ai_command` — never from this skill.

## When to Use

- The user has (or is about to write) a **requirements document** and wants a
  system built from it without supervising each stage
- Trigger phrases:
  - "Build this from the requirements doc"
  - "Greenfield this project"
  - "要件定義から自動構築して"
  - "Write requirements, walk away — make it happen"
  - "Resume the greenfield run" / "What happened to the autopilot?"
- The project is empty or freshly initialized (no meaningful source yet)

Do NOT use this for:

- Evolving an existing CoDD project — use `codd-evolve`
- Pure bug fix on an existing system — use `codd fix` / `codd fix [PHENOMENON]`
- Reverse-engineering an undocumented codebase — use `codd extract` / `codd brownfield`

## Decision Tree

```
1. Does a requirements file exist?
   ├─ yes → continue
   └─ no  → STOP-AND-ASK gate 1: ask the user for the path, or offer to
            draft docs/requirements/requirements.md from their description
            (they approve the draft before the autopilot starts)

2. Is the project already codd-initialized (codd/codd.yaml or .codd/codd.yaml)?
   ├─ no  → need --project-name and --language for codd init
   │        ambiguous? → STOP-AND-ASK gate 2
   └─ yes → does .codd/greenfield_session.yaml exist with a non-success result?
            ├─ yes → this is a RESUME: codd greenfield --resume
            └─ no  → fresh run; if source files already exist that a fresh
                     run could overwrite → STOP-AND-ASK gate 3

3. Run the canonical flow (below). Walk away.
```

## Canonical Flow (preferred)

One command. Prefer this over stage-by-stage unless recovering:

```bash
codd greenfield --requirements docs/requirements/requirements.md \
    [--project-name NAME --language LANG] \
    [--ntfy-topic TOPIC] [--max-repair-attempts 10]
```

- All gates are auto-approved (elicit findings applied, derived tasks and
  implementation steps approved, repair runs in automatic mode).
- `--ntfy-topic` posts progress notifications — notify-only, never blocking.
- `--dry-run` first when the user wants to preview the plan without spending
  AI calls.
- Checkpoints land in `.codd/greenfield_session.yaml` after every unit
  (every wave, every implement task), so interruption is cheap.
- Exit 0 = success and `codd check` passed. Non-zero = a stage failed; the
  report names the failed stage and the resume command.

## Stage-by-Stage Fallback (partial / recovery runs)

Use only when the one-command form is unsuitable: re-running a single failed
stage, splicing into CI, or debugging. The CLI sequence is the exact
equivalent of the autopilot (also available as a heavily-commented script at
`examples/greenfield_autopilot.sh` in the CoDD repository):

```bash
codd init NAME --language LANG --requirements FILE --auto-approve  # skip if initialized
codd elicit && codd elicit apply findings.md                       # advisory
codd plan --init --force
codd generate --all-waves --force
codd implement list-tasks --format json                            # then per task T:
codd implement plan  --task T                                      #   advisory
codd implement steps --task T --approve --all                      #   advisory
codd implement run   --task T                                      #   blocking
codd verify --auto-repair --max-attempts 10 --repair-mode automatic
codd propagate --verify && codd propagate --commit                 # advisory
codd check                                                         # final gate
```

### Resume and session inspection

```bash
codd greenfield --resume          # re-runs the first incomplete stage,
                                  # skipping units already marked done
cat .codd/greenfield_session.yaml # per-stage / per-unit status, failed_stage,
                                  # failed_unit, error
```

Stages are idempotent (generate skips existing files, implement re-runs are
safe), so resuming is always safe.

## Stop-and-Ask Gates

Ask the user **only** when one of these fires. Everything else is
auto-approved — that is the point of the autopilot.

1. **Missing requirements file.** No file at the given (or default) path.
   Ask for the path or offer to draft one from the user's description.
2. **Ambiguous project name / language.** Project is not initialized and the
   user gave neither. Ask once: "Project name and primary language?"
3. **Destructive re-init.** The directory already contains source files or a
   CoDD config that a fresh run could overwrite, and the session file does
   not indicate a resumable run. Confirm before proceeding (or steer to
   `codd greenfield --resume` / `codd-evolve` as appropriate).

**Do not** ask the user about: which lexicons to pick, whether to apply
elicit findings, task or step approval, repair proposals, wave order, or
commit messages for propagate. The autopilot auto-approves all of these.

## While the Autopilot Runs

**Do not interrupt the autopilot mid-run.** Do not kill the process, do not
run additional `codd` commands in the same project, and do not edit the
generated files while it is running — concurrent mutation corrupts the run.
To observe progress, inspect the checkpoint file (read-only):

```bash
cat .codd/greenfield_session.yaml
```

If the user wants progress pings, prefer `--ntfy-topic` over polling.
If the run looks stuck, let the stage time out and fail; then report the
failed stage from the session file and offer `codd greenfield --resume`.

## Failure Handling

1. Read `.codd/greenfield_session.yaml`: `result.failed_stage`,
   `result.failed_unit`, `result.error`.
2. The result report also prints an inspect command for the failed stage
   (e.g. `codd generate --wave 2`, `codd implement run --task T`) — run it
   for detail only if needed.
3. Transient failure (AI timeout, network): `codd greenfield --resume`.
4. Structural failure (requirements contradiction, unrepairable verify red):
   surface the error verbatim to the user; suggest fixing the requirements
   doc and resuming. Do not loop resume more than 2 times on the same error.

## Report

```
Greenfield autopilot: SUCCESS
  requirements: docs/requirements/requirements.md
  stages: init ✅  elicit ✅  plan ✅ (3 waves)  generate ✅
          implement ✅ (5 tasks)  verify ✅ (red 0)  propagate ⚠ (advisory)
          check ✅
  resume file: .codd/greenfield_session.yaml
```

On failure, name the failed stage/unit, the error, and the resume command.

## Absolute Constraints

1. **Never interrupt a running autopilot** — inspect the session file instead.
2. **Never hand-edit generated files mid-run.** After the run, changes go
   through `codd-evolve` / `codd fix`, not direct edits.
3. **Never re-init over an existing project without gate-3 confirmation.**
4. **Never report success without `codd check` passing** (the autopilot's
   own exit code already encodes this — trust it, don't re-judge).
5. **Never commit without user approval.** The autopilot builds; the user
   reviews and commits.

## Guardrails

- Use the `codd` command, not `python -m codd.cli`
- Run from the project root
- Prefer `codd greenfield` over the stage-by-stage fallback; the fallback is
  for recovery and CI splicing, not the default path
- One requirements doc per run; if the user has several, they import into the
  same project (`codd init --requirements` accepts one file; further docs go
  to `docs/requirements/` before running)

## Why This Skill Exists

CoDD's greenfield philosophy is "write only functional requirements and
constraints — the system builds itself." `codd greenfield` is that philosophy
as one command. This skill is the conversational front: it picks the right
entry (fresh run / resume / dry-run), enforces the three stop-and-ask gates,
and keeps humans from babysitting a pipeline that was designed to run
unattended. The CLI remains the engine; the skill decides when to start it
and how to read what it left behind.
