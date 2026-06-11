// ═══════════════════════════════════════════════════════════════════════════
// codd-greenfield.js — Claude Code Agent Workflow for the CoDD greenfield
// autopilot ("write requirements, walk away").
//
// WHAT THIS IS
//   A thin supervisor template for Claude Code's Agent Workflows. Each phase
//   spawns one agent that runs the corresponding `codd` stage command(s) via
//   Bash and reports structured status. The workflow stops at the first
//   failed phase and prints the resume command.
//
//   The workflow VEHICLE is Claude-Code-specific (it runs inside Claude
//   Code), but the UNDERLYING AI command that CoDD invokes for generation /
//   implementation / repair is whatever your project's codd.yaml `ai_command`
//   says — Claude CLI, Codex CLI, or any text-in/text-out CLI. This template
//   never assumes which one.
//
// SELF-CONTAINED EQUIVALENT
//   `codd greenfield --requirements <file>` runs the same pipeline as one
//   command, with checkpoint/resume via .codd/greenfield_session.yaml and
//   optional ntfy notifications. Use this workflow when you want Claude Code
//   to supervise the stages (visible per-phase progress, conversational
//   failure triage); use the CLI when you want a single detachable process.
//
// INSTALL
//   1. Copy this file into your project:  .claude/workflows/codd-greenfield.js
//   2. Make sure `codd` is on PATH and the project's codd.yaml has a working
//      `ai_command` (or rely on the default).
//
// RUN
//   In Claude Code, invoke the workflow with the requirements path as args:
//     /workflows codd-greenfield docs/requirements/requirements.md
//   Optional second/third args: project name and language (used only when
//   the project is not yet codd-initialized).
//
// RESUME AFTER A FAILURE
//   Failed runs leave per-stage state in .codd/greenfield_session.yaml.
//   Either re-run this workflow (every stage is idempotent: generate skips
//   existing files, implement re-runs are safe) or run the one-command form:
//     codd greenfield --resume
// ═══════════════════════════════════════════════════════════════════════════

export const meta = {
  name: "codd-greenfield",
  description:
    "CoDD greenfield autopilot: requirements doc in, working system out. " +
    "Runs init → elicit → plan → generate → implement → verify → propagate → check.",
  phases: [
    "init",
    "elicit",
    "plan",
    "generate",
    "implement",
    "verify",
    "propagate",
    "check",
  ],
};

// Shared instructions for every stage agent: keep them on rails.
const COMMON = `
You are one stage of the CoDD greenfield autopilot. Rules:
- Run ONLY the codd commands you are told to run, via Bash, from the project root.
- Do not edit files yourself; codd's own AI command does the generation.
- Do not ask the user questions; this is an unattended pipeline.
- End your reply with exactly one line: STATUS: OK or STATUS: FAIL — <one-line reason>.
`;

function failed(result) {
  // Structured-status contract: an agent reports failure via its final
  // STATUS line; a thrown/aborted agent counts as failure too.
  const text = String(result ?? "");
  return !/STATUS:\s*OK/.test(text);
}

export default async function run({ args, agent, phase, log }) {
  const [requirements, projectName = "my-project", language = "python"] =
    String(args ?? "").trim().split(/\s+/).filter(Boolean);

  if (!requirements) {
    log("Usage: /workflows codd-greenfield <requirements.md> [project-name] [language]");
    log("Self-contained equivalent: codd greenfield --requirements <requirements.md>");
    return;
  }

  const stages = [
    {
      name: "init",
      prompt: `${COMMON}
If codd/codd.yaml or .codd/codd.yaml already exists, do nothing and report OK.
Otherwise run:
  codd init ${projectName} --language ${language} --requirements ${requirements} --auto-approve`,
    },
    {
      name: "elicit",
      advisory: true,
      prompt: `${COMMON}
Run: codd elicit
If it succeeds and findings.md exists, also run: codd elicit apply findings.md
This stage is ADVISORY: report STATUS: OK even if the commands fail, but
mention the failure in your summary.`,
    },
    {
      name: "plan",
      prompt: `${COMMON}
Run: codd plan --init --force`,
    },
    {
      name: "generate",
      prompt: `${COMMON}
Run: codd generate --all-waves --force
It generates every wave in order and stops at the first failure.`,
    },
    {
      name: "implement",
      prompt: `${COMMON}
1. Run: codd implement list-tasks --format json
2. If the task list is empty, run: codd plan derive
   then approve all derived tasks with: codd plan approve <design-doc> --all
   for each design doc under docs/design/, and re-run list-tasks.
3. For EACH task id T, in listed order, run:
     codd implement plan --task T          (failure is non-blocking)
     codd implement steps --task T --approve --all   (failure is non-blocking)
     codd implement run --task T           (failure FAILS this phase)
Report how many tasks were implemented.`,
    },
    {
      name: "verify",
      prompt: `${COMMON}
Run: codd verify --auto-repair --max-attempts 10 --repair-mode automatic
This is the repair gate: report STATUS: FAIL if it exits non-zero.`,
    },
    {
      name: "propagate",
      advisory: true,
      prompt: `${COMMON}
Run: codd propagate --verify
Then: codd propagate --commit
This stage is ADVISORY on a fresh build ("nothing to propagate" is normal):
report STATUS: OK even if the commands fail, but mention it.`,
    },
    {
      name: "check",
      prompt: `${COMMON}
Run: codd check
This is the final health gate: report STATUS: FAIL if it exits non-zero.`,
    },
  ];

  for (const stage of stages) {
    await phase(stage.name);
    log(`[codd-greenfield] stage: ${stage.name}`);
    const result = await agent(stage.prompt);
    if (!stage.advisory && failed(result)) {
      log(`[codd-greenfield] stage ${stage.name} FAILED.`);
      log(`[codd-greenfield] inspect: .codd/greenfield_session.yaml (if present)`);
      log(`[codd-greenfield] resume:  re-run this workflow (stages are idempotent)`);
      log(`[codd-greenfield] or use:  codd greenfield --resume`);
      return;
    }
  }

  log("[codd-greenfield] SUCCESS — requirements in, system out. Final gate: codd check passed.");
}
