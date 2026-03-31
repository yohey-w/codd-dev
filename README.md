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

**Public Alpha** — `init` / `scan` / `impact` are stable; `validate` is alpha.

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

```bash
pip install codd-dev
mkdir my-project && cd my-project && git init

# Initialize — pass your requirements file, any format works
codd init --project-name "my-project" --language "typescript" \
  --requirements spec.txt

# AI generates design docs (wave_config auto-generated)
codd generate --wave 2

# Build dependency graph → analyze impact
codd scan
codd impact
```

## 5-Minute Demo — See CoDD in Action

We'll build **TaskFlow**, a task management app. Write **requirements in plain text**, let CoDD + AI handle everything else.

### Step 1: Write your requirements (any format — txt, md, doc)

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

Save this as `spec.txt`. That's it — no special formatting needed.

### Step 2: Initialize CoDD

```bash
pip install codd-dev
mkdir taskflow && cd taskflow && git init
codd init --project-name "taskflow" --language "typescript" \
  --requirements spec.txt
```

CoDD adds frontmatter (`node_id`, `type`, dependency metadata) automatically. You never touch it.

### Step 3: AI generates design docs

```bash
codd generate --wave 2   # System design + API design
codd generate --wave 3   # DB design + Auth design
codd generate --wave 4   # Test strategy
```

`wave_config` is auto-generated from your requirements. Each design doc gets frontmatter, `depends_on` declarations, and a `modules` field linking it to source code modules — all derived, nothing manual.

### Step 4: Build the dependency graph

```bash
codd scan
```

```
Frontmatter: 7 documents in docs
Scan complete:
  Documents with frontmatter: 7
  Graph: 7 nodes, 15 edges
  Evidence: 15 total (0 human, 15 auto)
```

7 docs, 15 dependency edges. Zero config written by hand.

### Step 5: Change requirements mid-project

Your PM asks for SSO and audit logging. Open `docs/requirements/requirements.md` and add:

```text
## Additional Requirements (v1.1)
- SAML SSO (enterprise customers)
- Audit logging (record & export all operations)
```

Save the file and ask CoDD what's affected:

```bash
codd impact    # detects uncommitted changes automatically
```

```
Changed files: 1
  - docs/requirements/requirements.md → req:taskflow-requirements

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

## Gray Band (informational)
| Target                  | Depth | Confidence |
|-------------------------|-------|------------|
| plan:implementation     | 2     | 0.00       |
```

**2 lines changed → 6 out of 7 docs affected.** Green band: AI auto-updates. Amber: human reviews. Gray: informational. You know exactly what to fix before anything breaks.

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
  plan_init: "claude --print --model claude-sonnet-4-6" # wave_config planning
  implement: "codex --print"                             # code generation
```

**Resolution priority**: CLI `--ai-cmd` flag > `ai_commands.{command}` > `ai_command` > built-in default (Opus).

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
| Modification | `codd extract` diff → `modules` search → identify affected docs → AI updates | Same flow |

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
| `codd extract` | **Alpha** | Reverse-engineer design docs from existing code |

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
      → Amber Band: "test-strategyが影響を受けています。更新しますか？"
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

See [docs/claude-code-setup.md](docs/claude-code-setup.md) for complete setup.

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
- [x] `codd verify` — language-agnostic verification (Python: mypy + pytest, TypeScript: tsc + jest)
- [ ] Multi-harness integration examples (Claude Code, Copilot, Cursor)
- [ ] VS Code extension for impact visualization

## Articles

- [Zenn (Japanese): CoDD deep-dive](https://zenn.dev/shio_shoppaize/articles/shogun-codd-coherence)
- [dev.to (English): What Happens After "Spec First"](https://dev.to/yohey-w/codd-coherence-driven-development-what-happens-after-spec-first-514f)

## License

MIT
