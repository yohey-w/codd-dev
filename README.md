<p align="center">
  <strong>CoDD — Coherence-Driven Development</strong><br>
  <em>The evidence engine for change management in AI-assisted development.</em>
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

> *When code changes, CoDD traces what's affected, checks what's violated, and produces the evidence trail for your merge decision.*

```
pip install codd-dev
```

**v1.7.0** — `init` / `scan` / `impact` are stable. `propagate` traces code changes to downstream design docs and doc-to-doc changes via CEG graph. `extract --ai` with baseline preset. Custom `node_id` prefixes via `codd.yaml`. GitHub Action for CI integration.

---

## Why CoDD?

AI can generate specs. But **what happens when upstream changes?**

Every spec-first tool stops at creation. CoDD starts there. When a requirement changes, code is updated, or a design assumption shifts, CoDD **automatically propagates the change downstream** — updating affected design docs, flagging stale artifacts, and producing an evidence trail.

```
Requirement changes → codd impact identifies 6 affected docs
Code changes        → codd propagate updates downstream designs
Design changes      → CEG graph traces all dependent artifacts
```

No other tool does this. spec-kit, Kiro, and cc-sdd create docs. **CoDD keeps them coherent.**

## How It Works

```
Requirements (human)  →  Design docs (AI)  →  Code & tests (AI)
         ↕                     ↕                     ↕
     codd impact         codd propagate        codd extract
    (what changed?)    (update downstream)   (reverse-engineer)
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
codd require              # Infer requirements from code (what was built and why)
codd plan --init          # Generate wave_config from extracted docs
codd scan                 # Build dependency graph
codd impact               # Change impact analysis
codd audit --skip-review  # Full change review: validate + impact + policy
codd measure              # Project health score (0-100)
```

## Demos

### Reproducible E2E Demo — 3 Propagation Patterns

The following demo is pinned to commit [`d7d9f45`](https://github.com/yohey-w/codd-dev/commit/d7d9f45). You can reproduce the full cycle locally.

**Setup:**
```bash
pip install codd-dev>=1.6.0
mkdir demo && cd demo && git init
cat > spec.txt << 'EOF'
TaskFlow — Requirements
- User authentication (email + Google OAuth)
- Workspace management (teams, roles, invites)
- Task CRUD with assignees, labels, due dates
- Real-time updates (WebSocket)
- File attachments (S3)
- Notification system (in-app + email)
EOF
codd init --project-name "taskflow" --language "typescript" --requirements spec.txt
```

**Pattern 1 — Source → Doc** (spec → design docs):
```bash
codd plan --init
for wave in $(seq 1 $(codd plan --waves)); do codd generate --wave $wave; done
codd validate        # Expected: PASS, 0 errors
codd scan            # Expected: 17 nodes, 30+ edges
```

**Pattern 2 — Doc → Doc** (requirement change → downstream update):
```bash
# Edit requirements: add "SSO (SAML 2.0)" to auth
codd impact          # Expected: 6/7 design docs in Green/Amber band

# Regenerate affected waves (propagate is for code→doc only)
codd generate --wave 1 --force   # Re-derive acceptance criteria from updated requirements
codd generate --wave 2 --force   # Re-derive system design from updated Wave 1
# Repeat for each affected wave in dependency order
```

**Pattern 3 — Doc → Doc via CEG** (code change → design update):
```bash
# Modify source code in auth module
codd propagate       # Expected: identifies auth-design, system-design as affected
codd propagate --update  # AI updates affected design docs from code diff
```

**Expected output**: 20-line spec → 17 design artifacts (5,100+ lines) → downstream propagation keeps all docs coherent after changes. Pattern 3 (CEG-based propagation) is novel — no other tool traces code changes back through the dependency graph to update design documents.

### Greenfield — Spec to Working App

37 lines of spec → 6 design docs (1,353 lines) → 102 code files (6,445 lines) → TypeScript strict build passes. No interactive AI chat — the entire workflow is a shell script.

Full walkthrough: [Harness as Code — A Guide to CoDD #1](https://zenn.dev/shio_shoppaize/articles/codd-greenfield-guide?locale=en)

### Brownfield — Change Impact Analysis

2 lines changed in requirements → `codd impact` identifies 6 out of 7 design docs affected. Green band: AI auto-updates. Amber band: human reviews. You know exactly what to fix before anything breaks.

Deep dive: [CoDD deep-dive](https://zenn.dev/shio_shoppaize/articles/shogun-codd-coherence?locale=en)

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

## Custom Node Prefixes

By default, `node_id` values must use one of the built-in prefixes (`design:`, `req:`, `doc:`, `module:`, etc.). To use CoDD for non-software domains (knowledge bases, review documents, prompt management), add custom prefixes in `codd.yaml`:

```yaml
# codd.yaml
prefixes:
  - knowledge
  - schema
  - review
  - prompt
```

Custom prefixes are **merged with** built-in defaults — you don't need to re-list `design`, `req`, etc. Prefix names must be lowercase letters and underscores only (`[a-z_]+`).

```yaml
# Now valid in frontmatter:
codd:
  node_id: "knowledge:domain-model"
```

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

Full walkthrough: [Harness as Code — A Guide to CoDD #2 Brownfield](https://zenn.dev/shio_shoppaize/articles/shogun-codd-brownfield?locale=en)

### AI-Powered Extraction (--ai)

> **Note on presets**: `codd extract --ai` ships with a **baseline** extraction prompt. The extraction quality in published benchmarks (F1 0.953+) was achieved with a tuned preset and internal evaluation dataset — not the public baseline. The baseline uses the same workflow and output format, but results will vary depending on your codebase and prompt. Use `--prompt-file` to supply your own tuned prompt.

```bash
codd extract --ai                        # Uses built-in baseline preset
codd extract --ai --prompt-file my.md    # Uses your custom prompt
```

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
| `codd verify` | Experimental (Pro) | V-Model verification |
| `codd implement` | Experimental | Design-to-code generation |
| `codd propagate` | **Alpha** | Propagate code/doc changes downstream to affected design docs |
| `codd review` | Experimental (Pro) | AI-powered artifact quality evaluation (LLM-as-Judge) |
| `codd extract` | **Alpha** | Reverse-engineer design docs from existing code |
| `codd require` | **Alpha** | Infer requirements from existing codebase (brownfield) |
| `codd audit` | **Alpha** (Pro) | Consolidated change review pack (validate + impact + policy + review) |
| `codd policy` | **Alpha** | Enterprise policy checker (forbidden/required patterns in source code) |
| `codd measure` | **Alpha** | Project health metrics (graph, coverage, quality, health score 0-100) |
| `codd mcp-server` | **Alpha** | MCP server for AI tool integration (stdio, zero dependencies) |

## OSS / Pro Split

CoDD v1.6.0 introduced a clean OSS/Pro boundary via a bridge pattern.

**OSS (MIT, free)** — everything you need to keep docs coherent:

`init` · `scan` · `impact` · `generate` · `restore` · `propagate` · `extract` · `require` · `plan` · `validate` · `measure` · `policy` · `mcp-server`

**Pro (private, paid)** — enterprise review and verification:

`review` · `verify` · `audit` · `risk`

```bash
# OSS only
pip install codd-dev

# Add Pro extensions
pip install "codd-pro @ git+ssh://git@github.com/yohey-w/codd-pro.git"
```

When `codd-pro` is installed, Pro implementations automatically override OSS fallbacks via entry-points plugin discovery. When it's not installed, Pro commands show a migration message and exit gracefully. No configuration needed.

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
| `codd_measure` | Project health metrics (graph, coverage, quality, health score) |

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

In short: spec-kit, Kiro, and cc-sdd answer *"how do I create specs?"* CoDD answers *"when something changes upstream, how do I automatically update everything downstream?"*

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
codd verify           # mypy + pytest (434 tests pass)
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
- [x] `codd require` — infer requirements from existing codebase with confidence tags
- [x] `codd audit` — consolidated change review pack (validate + impact + policy + review)
- [x] `codd policy` — enterprise policy checker (forbidden/required patterns)
- [x] `codd measure` — project health metrics (graph, coverage, quality, score 0-100)
- [x] GitHub Action — CI integration for PR audit with auto-commenting
- [x] MCP Server — stdio JSON-RPC server for AI tool integration
- [x] Plugin system — extensible require prompts (tags, evidence format, output sections)
- [ ] Multi-harness integration examples (Claude Code, Copilot, Cursor)
- [ ] VS Code extension for impact visualization

## Articles

- [dev.to: Harness as Code — Treating AI Workflows Like Infrastructure](https://dev.to/yohey-w/harness-as-code-treating-ai-workflows-like-infrastructure-27ni)
- [dev.to: What Happens After "Spec First"](https://dev.to/yohey-w/codd-coherence-driven-development-what-happens-after-spec-first-514f)
- [Zenn: Harness as Code — A Guide to CoDD #1 spec → design → code](https://zenn.dev/shio_shoppaize/articles/codd-greenfield-guide?locale=en)
- [Zenn: Harness as Code — A Guide to CoDD #2 Brownfield](https://zenn.dev/shio_shoppaize/articles/shogun-codd-brownfield?locale=en)
- [Zenn: Harness as Code — A Guide to CoDD #3 Bug Fixing with CoDD extract (SWE-bench)](https://zenn.dev/shio_shoppaize/articles/codd-swebench-pilot?locale=en)
- [Zenn: CoDD deep-dive](https://zenn.dev/shio_shoppaize/articles/shogun-codd-coherence?locale=en)

## License

MIT
