# Fixed context an AI CLI adds to every call

CoDD starts an AI CLI as a subprocess for each call. Before your prompt, the CLI
sends its own fixed context: system instructions, tool definitions, the listing
of installed skills and plugins, and any connected external tools. That cost
repeats on every call — and on every round trip of an agentic run — so it is
worth knowing its size.

## Measure it

- Run the CLI once with a five-word prompt in an empty directory and read the
  input tokens it reports (Claude Code: `--output-format json`; Codex CLI:
  `--json`).
- `codd usage summary` (opt-in — set `ai.usage_log` or `CODD_AI_USAGE_LOG`)
  shows calls, characters and elapsed time per stage. It records characters,
  not tokens.

## Measured examples

One machine, 2026-11, prompt `Reply with exactly: OK`. Your numbers depend on
what you have installed and connected.

| CLI and options | Input tokens |
|---|---|
| Claude Code 2.1, default tool set (450 MCP tools connected, deferred) | 59,285 |
| Claude Code 2.1, `--tools ""` (the same MCP tools, no longer deferred) | 421,162 |
| Claude Code 2.1, `--tools "" --strict-mcp-config` | 18,825 |
| Claude Code 2.1, `--tools "" --strict-mcp-config --setting-sources project` | 3,776 |
| Codex CLI 0.171, default | 27,482 |
| Codex CLI 0.171, `-c skills.max_context_tokens=1` | 16,779 |
| Codex CLI 0.171, the above plus `--disable` for unused features | 14,401 |

The Claude rows use one model throughout; the Codex rows use one model
throughout.

## What CoDD does, and what it leaves to you

- For a Claude command with an explicit empty list (`--tools ""`), CoDD adds
  `--strict-mcp-config` unless you already pass an MCP flag. See the README.
- CoDD does not add `--setting-sources`, `--safe-mode`, or any Codex option for
  you. They depend on the CLI version and can drop settings you rely on
  (provider environment variables, hooks). Put them in your own `ai_command` /
  `ai_commands` after checking `--help` for your version.
- The flags above are examples from the versions named in the table. Verify
  them against your CLI before copying them.

## Route self-contained implementation tasks to a separate command

In `codd/codd.yaml`, set `ai_commands.implement_one_shot` to a command that
reads the prompt on stdin and emits complete `=== FILE: <path> ===` blocks on
stdout. Configure any tool restrictions in the command itself. The default
value is `null`.

CoDD chooses the command for each task as it builds the first prompt. A
`--ai-cmd` override takes precedence. For one-shot routing, the task must name
concrete output files, show all existing outputs in full, and stay within
`implement.existing_output_files_prompt_limit`. If a derived plan is available,
CoDD must match the task to an approved entry by design document and overlapping
outputs. Tasks can still qualify without a derived plan.

CoDD requires full content for every Tier 1 dependency artifact. Tier 1 covers
the artifacts identified through imports from the task's existing files and
outputs from its immediate dependency tasks. An unresolved immediate dependency
or a missing, unreadable, or abbreviated Tier 1 artifact sends the task to the
ordinary command. Tier 2 covers the remaining transitive task dependencies and
design-document closure. Missing or abbreviated Tier 2 artifacts are counted for
logging and leave the task eligible. The task's own declared outputs are handled
by the existing-output rules.

This applies to task generation in `codd implement run` and greenfield,
including regeneration with feedback. Step derivation and chunked execution
use their existing commands.

After the one-shot command exhausts the existing retries for unusable output
or write-time validation, CoDD switches once to the ordinary implementation
command and runs its usual retries. Startup failures, timeouts, and nonzero
exit codes follow the invocation layer's existing error handling.

With the setting enabled, stderr reports each routing decision and any fallback.
If `ai.usage_log` is enabled, the JSONL log also records these with `event` set to
`implement_routing`. Each record contains `timestamp`, `stage`, `design_node`,
`outputs`, `route`, `reason`, `detail`, `fallback`, and
`incomplete_distant_artifacts`. The count reports the number of Tier 2 artifact
paths missing from the first prompt or shown without full content. Both stderr
and the JSONL log report that first-prompt count again on fallback.
`codd usage summary` counts only invocations.

### Example and one measurement

```yaml
ai_commands:
  # the ordinary command: an agent that can read and edit the project
  implement: codex exec --sandbox workspace-write --model <model>
  # the same CLI without shell access, so it cannot read project files
  implement_one_shot: codex exec --sandbox read-only --disable shell_tool --model <model>
```

One machine, 2026-11, Codex CLI 0.171, one model and one reasoning setting for
both rows, the same prompts. The project is a small Python sample whose detailed
design documents already contain most of the code. Three tasks, each writing one
new source file and its test, three runs each.

| Command | Round trips | Input tokens, cache reads included | Input without cache reads, plus output |
|---|---|---|---|
| ordinary (can read files) | 7–14 | 567k–1,500k | 68k–134k |
| without shell access (cannot read files) | 1 | 62k–73k | 52k–84k |

- The sample's own test suite (123 tests, not written by these runs) passed in
  all 18 runs.
- Nearly all of the difference is cache reads. How much that matters depends on
  how your plan or provider counts them.
- This is one implement call per task, not the total to a final green build,
  and a project whose design documents carry less code may behave differently.
- A command without file access cannot look at the project's other tests. In
  these runs it twice marked behaviors that belong to other modules as
  `codd: blocked`; the tests still passed. Read the verifiable-behavior audit
  as well as the test result.
- `--disable shell_tool` does not make this CLI tool-less: a code-execution
  tool remains. In the nine runs of that row it was never used. In one later run of the
  same task the model called it four times (five round trips, 316k input
  tokens with cache reads) and still produced the files.
- Check the flags against `--help` for your CLI version.
