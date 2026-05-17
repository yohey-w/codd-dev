# codd skills

`codd skills` installs bundled CoDD skills for Claude Code and Codex CLI.

## Install

Install `codd-evolve` for both agents:

```bash
codd skills install codd-evolve
```

This creates:

```text
~/.claude/skills/codd-evolve
~/.agents/skills/codd-evolve
```

Install for one target:

```bash
codd skills install codd-evolve --target claude
codd skills install codd-evolve --target codex
```

Install into the current repository instead of the user profile:

```bash
codd skills install codd-evolve --scope repo
```

Install by copying files instead of creating symlinks:

```bash
codd skills install codd-evolve --mode copy
```

Install from an explicit skill source during development:

```bash
codd skills install codd-evolve --dir ./skills/codd-evolve
```

If a different file, directory, or symlink already exists at the destination, the command fails. Use `--force` to rename the existing destination to a timestamped `.bak` path before installing.

## List

Show all user and repository skills:

```bash
codd skills list
```

Show only Codex user skills:

```bash
codd skills list --target codex --scope user
```

Machine-readable output:

```bash
codd skills list --format json
```

## Remove

Remove a skill from both user targets:

```bash
codd skills remove codd-evolve
```

Keep a timestamped backup while removing:

```bash
codd skills remove codd-evolve --keep-backup
```

Remove only the repository-local Codex install:

```bash
codd skills remove codd-evolve --target codex --scope repo
```
