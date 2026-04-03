<p align="center">
  <strong>CoDD — Coherence-Driven Development</strong><br>
  <em>Keep AI-built systems coherent when requirements change.</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/v/codd-dev?style=flat-square&color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/pyversions/codd-dev?style=flat-square" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License"></a>
  <a href="https://github.com/yohey-w/codd-dev/stargazers"><img src="https://img.shields.io/github/stars/yohey-w/codd-dev?style=flat-square" alt="Stars"></a>
</p>

<p align="center">
  <a href="README_ja.md">日本語</a> | English
</p>

---

> *Harnesses tell agents how to work. CoDD keeps artifacts coherent.*

```
pip install codd-dev
```

**v1.4.0** — `init` / `scan` / `impact` are stable. `audit` / `policy` / `require` / `extract` / `validate` are alpha. GitHub Action for CI integration.

---

## Why CoDD?

AI can generate code from specs. But what happens when **requirements change mid-project?**

- Which design docs are affected?
- Which tests need updating?
- Which API contracts broke?
- Did anyone forget to update the database migration?

**Spec Kit** and **OpenSpec** answer *"how do I start?"*
**CoDD** answers *"how do I keep going when things change?"*

## How It Works

```
Requirements (human)  →  Design docs (AI)  →  Code & tests (AI)
                              ↑
                    codd scan builds the
                     dependency graph
                              ↓
            Something changes? codd impact tells you
             exactly what's affected — automatically.
```

### The Three Layers

```
Harness (CLAUDE.md, Hooks, Skills)   ← Rules, guardrails, workflow
  └─ CoDD (methodology)              ← Coherence across changes
       └─ Design docs (docs/*.md)    ← Artifacts CoDD manages
```

CoDD is **harness-agnostic** — works with Claude Code, Copilot, Cursor, or any agent framework.

## Core Principle: Derive, Don't Configure

| Architecture | Derived test strategy | Config needed? |
|---|---|---|
| Next.js + Supabase | vitest + Playwright | None |
| FastAPI + Python | pytest + httpx | None |
| CLI tool in Go | go test | None |

**Upstream determines downstream.** You define requirements and constraints. AI derives everything else.

## Quick Start

### Greenfield (new project)

```bash
pip install codd-dev
mkdir my-project && cd my-project && git init

# Initialize — pass your requirements file, any format works
codd init --project-name "my-project" --language "typescript" \
  --requirements spec.txt

# AI designs the document dependency graph
codd plan --init

# Generate design docs wave by wave
waves=$(codd plan --waves)
for wave in $(seq 1 $waves); do
  codd generate --wave $wave
done

# Quality gate — catch AI laziness (TODOs, placeholders)
codd validate

# Generate code from design docs
sprints=$(codd plan --sprints)
for sprint in $(seq 1 $sprints); do
  codd implement --sprint $sprint
done

# Assemble code fragments into a buildable project
codd assemble
```

### Brownfield (existing project)

```bash
codd extract              # Reverse-engineer design docs from code
codd plan --init          # Generate wave_config from extracted docs
codd scan                 # Build dependency graph
codd impact               # Change impact analysis
```

## 5-Minute Greenfield Demo — Spec to Working App

37 lines of spec → 6 design docs (1,353 lines) → 102 code files (6,445 lines) → TypeScript strict build passes.

### Step 1: Write your requirements

```text
# TaskFlow — Personal Todo App

## Functional Requirements
- Task CRUD: create, read, update, delete tasks
- Each task has: title, description (optional), due date (optional),
  priority (low/medium/high), completed status
- Task list with filtering by: status (all/active/completed), priority
- Local state management (no backend, localStorage)

## UI Requirements
- Single-page app with responsive layout (mobile-first)
- Dark theme with accent color (#3b82f6)
- Floating action button opens a modal form
- Toast notifications on create/update/delete
- Keyboard shortcuts: Enter to submit, Escape to close modal

## Constraints
- Next.js 15 App Router with React Server Components
- Tailwind CSS
- TypeScript strict mode
- Deploy-ready as static export
```

### Step 2: Run the pipeline

```bash
pip install codd-dev
codd init --requirements spec.md
codd plan --init                          # AI designs the wave structure

waves=$(codd plan --waves)                # → 4
for wave in $(seq 1 $waves); do
  codd generate --wave $wave              # design docs, wave by wave
done

codd validate                             # quality gate

sprints=$(codd plan --sprints)            # → 17
for sprint in $(seq 1 $sprints); do
  codd implement --sprint $sprint         # code from design docs
done

codd assemble                             # integrate into buildable project
npm run build                             # TypeScript strict, zero errors
```

No interactive AI chat at any step. Every AI call goes through `claude --print` — prompt in, text out. **Harness as Code**: the entire workflow is a shell script.

### Step 3: Model role separation

```bash
# Design docs — needs judgment, use Opus
codd generate --wave 1 --ai-cmd 'claude --print --model claude-opus-4-6 --tools ""'

# Code generation — needs volume, use Codex (or Sonnet)
codd implement --sprint 1 --ai-cmd 'codex --full-auto -q'
```

## 5-Minute Brownfield Demo — Change Impact Analysis

Already have a codebase? CoDD tracks what's affected when requirements change.

### Step 1: Write requirements and generate design docs

```text
# TaskFlow — Requirements

## Functional Requirements
- User auth (email + Google OAuth)
- Workspace management (teams, roles, invites)
- Task CRUD with assignees, labels, due dates
- Real-time updates (WebSocket)
- File attachments (S3)
- Notification system (in-app + email)

## Constraints
- Next.js + Prisma + PostgreSQL
- Row-level security for workspace isolation
- All API endpoints rate-limited
```

```bash
codd init --requirements spec.txt
codd plan --init
waves=$(codd plan --waves)
for wave in $(seq 1 $waves); do codd generate --wave $wave; done
codd scan
```

```
Scan complete:
  Documents with frontmatter: 7
  Graph: 7 nodes, 15 edges
```

### Step 2: Change requirements mid-project

Your PM asks for SSO and audit logging. Add to `docs/requirements/requirements.md`:

```text
## Additional Requirements (v1.1)
- SAML SSO (enterprise customers)
- Audit logging (record & export all operations)
```

```bash
codd impact    # detects uncommitted changes automatically
```

```
# CoDD Impact Report

## Green Band (high confidence, auto-propagate)
| Target                  | Depth | Confidence |
|-------------------------|-------|------------|
| design:system-design    | 1     | 0.90       |
| design:api-design       | 1     | 0.90       |
| detail:db-design        | 2     | 0.90       |
| detail:auth-design      | 2     | 0.90       |

## Amber Band (must review)
| Target                  | Depth | Confidence |
|-------------------------|-------|------------|
| test:test-strategy      | 2     | 0.90       |
```

**2 lines changed → 6 out of 7 docs affected.** Green band: AI auto-updates. Amber: human reviews. You know exactly what to fix before anything breaks.

## Wave-Based Generation

Design docs are generated in dependency order — each Wave depends on the previous:

```
Wave 1  Acceptance criteria + ADR       ← requirements only
Wave 2  System design                   ← req + Wave 1
Wave 3  DB design + API design          ← req + Wave 1-2
Wave 4  UI/UX design                    ← req + Wave 1-3
Wave 5  Implementation plan             ← all above
```

Verification runs bottom-up (V-Model):

```
Unit tests        ← verifies detailed design
Integration       ← verifies system design
E2E / System      ← verifies requirements + acceptance criteria
```

## Frontmatter = Single Source of Truth

Dependencies are declared in Markdown frontmatter. No separate config files.

```yaml
---
codd:
  node_id: "design:api-design"
  modules: ["api", "auth"]        # ← links to source code modules
  depends_on:
    - id: "design:system-design"
      relation: derives_from
    - id: "req:my-project-requirements"
      relation: implements
---
```

The `modules` field enables reverse traceability: when source code changes, `codd extract` identifies affected modules, and the `modules` field maps those modules back to the design docs that need updating.

`codd/scan/` is a cache — regenerated on every `codd scan`.

## AI Model Configuration

CoDD calls an external AI CLI for document generation. The default is Claude Opus:

```yaml
# codd.yaml
ai_command: "claude --print --model claude-opus-4-6"
```

### Per-Command Override

Different commands can use different models. For example, use Opus for design doc generation but Codex for code implementation:

```yaml
ai_command: "claude --print --model claude-opus-4-6"   # global default
ai_commands:
  generate: "claude --print --model claude-opus-4-6"    # design doc generation
  restore: "claude --print --model claude-opus-4-6"     # brownfield reconstruction
  review: "claude --print --model claude-opus-4-6"      # quality evaluation
  plan_init: "claude --print --model claude-sonnet-4-6" # wave_config planning
  implement: "codex --print"                             # code generation
```

**Resolution priority**: CLI `--ai-cmd` flag > `ai_commands.{command}` > `ai_command` > built-in default (Opus).

### Claude Code Context Interference

When `claude --print` runs inside a project directory, it auto-discovers `CLAUDE.md` and loads project-level system prompts. These instructions can conflict with CoDD's generation prompts, causing format validation failures like:

```
Error: AI command returned unstructured summary for 'ADR: ...'; missing section headings
```

**Fix**: Use `--system-prompt` to override project context with a focused instruction:

```yaml
ai_command: "claude --print --model claude-opus-4-6 --system-prompt 'You are a technical document generator. Output only the requested Markdown document. Follow section heading instructions exactly.'"
```

> **Note**: `--bare` strips all context but also disables OAuth authentication. Use `--system-prompt` instead — it overrides `CLAUDE.md` while preserving auth.

## Config Directory Discovery

By default, `codd init` creates a `codd/` directory. If your project already has a `codd/` directory (e.g., it's your source code package), use `--config-dir`:

```bash
codd init --config-dir .codd --project-name "my-project" --language "python"
```

All other commands (`scan`, `impact`, `generate`, etc.) automatically discover whichever config directory exists — `codd/` first, then `.codd/`. No extra flags needed.

## Brownfield? Start Here

Already have a codebase? CoDD provides a full brownfield workflow — from code extraction to design doc reconstruction.

### Step 1: Extract structure from code

`codd extract` reverse-engineers design documents from your source code. No AI required — pure static analysis.

```bash
cd existing-project
codd extract
```

```
Extracted: 13 modules from 45 files (12,340 lines)
Output: codd/extracted/
  system-context.md     # Module map + dependency graph
  modules/auth.md       # Per-module design doc
  modules/api.md
  modules/db.md
  ...
```

### Step 2: Generate wave_config from extracted docs

`codd plan --init` automatically detects extracted docs and generates a wave_config — no requirement docs needed.

```bash
codd plan --init    # Detects codd/extracted/, builds brownfield wave_config
```

Each artifact in the generated wave_config includes a `modules` field linking it to source code modules — enabling reverse traceability from code changes back to design docs.

### Step 3: Restore design documents

`codd restore` reconstructs design documents from extracted facts. Unlike `codd generate` (which creates docs from requirements), `restore` asks *"what IS the current design?"* — reconstructing intent from code structure.

```bash
codd restore --wave 2   # Reconstruct system design from extracted facts
codd restore --wave 3   # Reconstruct DB/API design
```

### Step 4: Build the graph

```bash
codd scan
codd impact
```

**Philosophy**: In V-Model, intent lives only in requirements. Architecture, design, and tests are structural facts — extractable from code. `codd extract` gets the structure; `codd restore` reconstructs the design; you add the "why" later.

### Greenfield vs Brownfield

| | Greenfield | Brownfield |
|--|-----------|-----------|
| Starting point | Requirements (human-written) | Existing codebase |
| Planning | `codd plan --init` (from requirements) | `codd plan --init` (from extracted docs) |
| Doc generation | `codd generate` (forward: requirements → design) | `codd restore` (backward: code facts → design) |
| Traceability | `modules` field links docs → code | `modules` field links docs → code |
| Modification | `codd propagate` (code → affected docs → optional AI update) | Same flow |

## Commands

| Command | Status | Description |
|---------|--------|-------------|
| `codd init` | **Stable** | Initialize CoDD in any project (`--config-dir .codd` for projects where `codd/` exists) |
| `codd scan` | **Stable** | Build dependency graph from frontmatter |
| `codd impact` | **Stable** | Change impact analysis (Green / Amber / Gray) |
| `codd validate` | **Alpha** | Frontmatter integrity & graph consistency check |
| `codd generate` | Experimental | Generate design docs in Wave order (greenfield) |
| `codd restore` | Experimental | Reconstruct design docs from extracted facts (brownfield) |
| `codd plan` | Experimental | Wave execution status (`--init` supports brownfield fallback) |
| `codd verify` | Experimental | V-Model verification |
| `codd implement` | Experimental | Design-to-code generation |
| `codd propagate` | Experimental | Reverse-propagate source code changes to design docs |
| `codd review` | Experimental | AI-powered artifact quality evaluation (LLM-as-Judge) |
| `codd extract` | **Alpha** | Reverse-engineer design docs from existing code |
| `codd require` | **Alpha** | Infer requirements from existing codebase (brownfield) |
| `codd audit` | **Alpha** | Consolidated change review pack (validate + impact + policy + review) |
| `codd policy` | **Alpha** | Enterprise policy checker (forbidden/required patterns in source code) |

## CI Integration (GitHub Action)

Run CoDD audit on every pull request. The action posts a comment with verdict (APPROVE / CONDITIONAL / REJECT), validation results, policy violations, and impact analysis.

### Quick Setup

Add `.github/workflows/codd.yml` to your project:

```yaml
name: CoDD Audit
on:
  pull_request:
    branches: [main]

permissions:
  contents: read
  pull-requests: write

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: yohey-w/codd-dev@main
        with:
          diff-target: origin/${{ github.base_ref }}
          skip-review: "true"  # Set to "false" to enable AI review
```

### Action Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `diff-target` | `origin/main` | Git ref to diff against |
| `skip-review` | `true` | Skip AI review phase (faster, no AI cost) |
| `python-version` | `3.12` | Python version |
| `codd-version` | latest | Specific version (e.g., `>=1.3.0`) |
| `post-comment` | `true` | Post results as PR comment |

### Action Outputs

| Output | Description |
|--------|-------------|
| `verdict` | `APPROVE`, `CONDITIONAL`, or `REJECT` |
| `risk-level` | `LOW`, `MEDIUM`, or `HIGH` |
| `report-json` | Path to the JSON audit report |

### Enterprise Policies

Define source code policies in your `codd.yaml`:

```yaml
policies:
  - id: SEC-001
    description: "No hardcoded passwords"
    severity: CRITICAL
    kind: forbidden
    pattern: 'password\s*=\s*[''"]'
    glob: "*.py"

  - id: LOG-001
    description: "All modules must import logging"
    severity: WARNING
    kind: required
    pattern: "import logging"
    glob: "*.py"
```

The policy checker runs as part of `codd audit` and independently via `codd policy`. Critical violations cause REJECT; warnings cause CONDITIONAL.

## MCP Server

CoDD exposes its tools via the [Model Context Protocol](https://modelcontextprotocol.io/) for direct AI tool integration. Zero external dependencies — works with any MCP-compatible client.

```bash
codd mcp-server --project /path/to/your/project
```

### Claude Code Configuration

Add to `~/.claude/claude_code_config.json`:

```json
{
  "mcpServers": {
    "codd": {
      "command": "codd",
      "args": ["mcp-server", "--project", "/path/to/your/project"]
    }
  }
}
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `codd_validate` | Check frontmatter integrity and graph consistency |
| `codd_impact` | Analyze change impact for a given node or file |
| `codd_policy` | Check source code against enterprise policy rules |
| `codd_audit` | Consolidated change review (validate + impact + policy) |
| `codd_scan` | Build dependency graph from design documents |

## Claude Code Integration

CoDD ships with slash-command Skills for Claude Code. Instead of running CLI commands yourself, use Skills — Claude reads the project context and runs the right command with the right flags.

### Skills Demo — Same TaskFlow App, Zero CLI

```
You:  /codd-init
      → Claude: codd init --project-name "taskflow" --language "typescript" \
                  --requirements spec.txt

You:  /codd-generate
      → Claude: codd generate --wave 2 --path .
      → Claude reads every generated doc, checks scope, validates frontmatter
      → "Wave 2の設計書を確認しました。Wave 3に進みますか？"

You:  yes

You:  /codd-generate
      → Claude: codd generate --wave 3 --path .

You:  /codd-scan
      → Claude: codd scan --path .
      → Reports: "7 documents, 15 edges. No warnings."

You:  (edit requirements — add SSO + audit logging)

You:  /codd-impact
      → Claude: codd impact --path .
      → Green Band: auto-updates system-design, api-design, db-design, auth-design
      → Amber Band: "test-strategy is affected. Update it?"

You:  (modify source code — implement the SSO feature)

You:  /codd-propagate
      → Claude: codd propagate --path .
      → "3 files changed in auth module. 2 design docs affected:
         design:system-design, design:auth-detail"
      → "Run with --update to update these docs?"

You:  yes
      → Claude: codd propagate --path . --update
      → Reviews updated docs, confirms changes are accurate
```

**Key difference**: Skills add human-in-the-loop gates. `/codd-generate` pauses between waves for approval. `/codd-impact` follows the Green/Amber/Gray protocol — auto-updating safe changes, asking before risky ones.

### Hook Integration — Set It Once, Never Think Again

Add this hook and **you never run `codd scan` manually again.** Every file edit triggers it automatically — the dependency graph is always current, always accurate, zero mental overhead:

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{
        "type": "command",
        "command": "codd scan --path ."
      }]
    }]
  }
}
```

With hooks active, your entire workflow becomes: **edit files normally, then run `/codd-impact` when you want to know what's affected.** That's it. The graph maintenance is invisible.

### Available Skills

| Skill | What it does |
|-------|-------------|
| `/codd-init` | Initialize + import requirements |
| `/codd-generate` | Generate design docs wave-by-wave with HITL gates (greenfield) |
| `/codd-restore` | Reconstruct design docs from extracted code facts (brownfield) |
| `/codd-scan` | Rebuild dependency graph |
| `/codd-impact` | Change impact analysis with Green/Amber/Gray protocol |
| `/codd-validate` | Frontmatter & dependency consistency check |
| `/codd-propagate` | Reverse-propagate source code changes to design docs |
| `/codd-review` | AI quality review with PASS/FAIL verdict and feedback |

See [docs/claude-code-setup.md](docs/claude-code-setup.md) for complete setup.

## Autonomous Quality Loop

`codd review` evaluates artifacts using AI (LLM-as-Judge), and `--feedback` feeds results back into generation. Together they enable a fully autonomous quality loop:

```bash
# Generate → Review → Regenerate with feedback until PASS
codd generate --wave 2 --force
feedback=$(codd review --path . --json | jq -r '.results[0].feedback')
verdict=$(codd review --path . --json | jq -r '.results[0].verdict')

while [ "$verdict" = "FAIL" ]; do
  codd generate --wave 2 --force --feedback "$feedback"
  result=$(codd review --path . --json)
  verdict=$(echo "$result" | jq -r '.results[0].verdict')
  feedback=$(echo "$result" | jq -r '.results[0].feedback')
done
```

Review criteria are type-specific:

| Doc Type | Criteria |
|----------|----------|
| Requirement | Completeness, consistency, testability, ambiguity |
| Design | Architecture soundness, API quality, security, upstream consistency |
| Detailed Design | Implementation clarity, data model, error handling, interface contracts |
| Test | Coverage, edge cases, independence, traceability |

**Scoring**: 80+ = PASS. CRITICAL issues auto-cap at 59. Exit code 1 on FAIL — loop-friendly.

**Model allocation**: Use Opus for review (`ai_commands.review`), Codex for implementation (`ai_commands.implement`). The `ai_commands` config makes this a one-line change.

## How CoDD Differs from Other Spec-Driven Tools

All major spec-driven tools focus on **creating** design documents. None address what happens when those documents **change**. CoDD fills that gap with a dependency graph, impact analysis, and a band-based update protocol.

| | **spec-kit** (GitHub) | **Kiro** (AWS) | **cc-sdd** (gotalab) | **CoDD** |
|--|---|---|---|---|
| Focus | Spec creation (req -> design -> tasks -> code) | Agentic IDE with native SDD pipeline | Kiro-style SDD for Claude Code | **Post-creation coherence maintenance** |
| Stars | 83.7k | N/A (proprietary IDE) | 3k | -- |
| Change propagation | No | No | No | **`codd impact` + dependency graph** |
| Impact analysis | No | No | No | **Green / Amber / Gray bands** |
| Spec notation | Markdown + 40 extensions | EARS notation | Quality gates + git worktree | Frontmatter `depends_on` |
| Harness lock-in | GitHub Copilot | Kiro IDE | Claude Code | **Any agent / IDE** |

In short: spec-kit, Kiro, and cc-sdd answer *"how do I create specs?"* CoDD answers *"how do I keep specs, code, and tests coherent when requirements change?"*

## Comparison

|  | Spec Kit | OpenSpec | **CoDD** |
|--|----------|---------|----------|
| Spec-first generation | Yes | Yes | Yes |
| **Change propagation** | No | No | **Dependency graph + impact analysis** |
| **Derive test strategy** | No | No | **Automatic from architecture** |
| **V-Model verification** | No | No | **Unit → Integration → E2E** |
| **Impact analysis** | No | No | **`codd impact`** |
| Harness-agnostic | Copilot focused | Multi-agent | **Any harness** |

## Real-World Usage

Battle-tested on a production web app — 18 design docs connected by a dependency graph. All docs, code, and tests generated by AI following CoDD. When requirements changed mid-project, `codd impact` identified affected artifacts and AI fixed them automatically.

```
docs/
├── requirements/       # What to build (human input — plain text)
├── design/             # System design, API, DB, UI (AI-generated)
├── detailed_design/    # Module-level specs (AI-generated)
├── governance/         # ADRs (AI-generated)
├── plan/               # Implementation plan
├── test/               # Acceptance criteria, test strategy
├── operations/         # Runbooks
└── infra/              # Infrastructure design
```

### CoDD Manages Its Own Development

CoDD dogfoods itself. The `.codd/` directory contains CoDD's own config, and `codd extract` reverse-engineers design docs from its own source code. The full V-Model lifecycle runs on itself:

```bash
codd init --config-dir .codd --project-name "codd-dev" --language "python"
codd extract          # 15 modules → design docs with dependency frontmatter
codd scan             # 49 nodes, 83 edges
codd verify           # mypy + pytest (127/127 tests pass)
```

If CoDD can't manage itself, it shouldn't manage your project.

## Roadmap

- [ ] Semantic dependency types (`requires`, `affects`, `verifies`, `implements`)
- [x] `codd extract` — reverse-generate design docs from existing codebases (brownfield support)
- [x] `codd restore` — reconstruct design docs from extracted facts (brownfield doc generation)
- [x] `codd plan --init` brownfield fallback — generate wave_config from extracted docs
- [x] `modules` field — design doc ↔ source code traceability
- [x] Per-command AI model configuration (`ai_commands` in codd.yaml)
- [x] `codd propagate` — reverse-propagate source code changes to design documents
- [x] `codd review` — AI-powered quality evaluation with review-driven regeneration loop
- [x] `--feedback` flag — feed review results back into generate/restore/propagate
- [x] `codd verify` — language-agnostic verification (Python: mypy + pytest, TypeScript: tsc + jest)
- [ ] Multi-harness integration examples (Claude Code, Copilot, Cursor)
- [ ] VS Code extension for impact visualization

## Articles

- [dev.to (English): Harness as Code — Treating AI Workflows Like Infrastructure](https://dev.to/yohey-w/harness-as-code-treating-ai-workflows-like-infrastructure-27ni)
- [dev.to (English): What Happens After "Spec First"](https://dev.to/yohey-w/codd-coherence-driven-development-what-happens-after-spec-first-514f)
- [Zenn (Japanese): Harness as Code — CoDD活用ガイド #1 spec → 設計書 → コード](https://zenn.dev/shio_shoppaize/articles/codd-greenfield-guide)
- [Zenn (Japanese): CoDD deep-dive](https://zenn.dev/shio_shoppaize/articles/shogun-codd-coherence)

## License

MIT
