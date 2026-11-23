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
