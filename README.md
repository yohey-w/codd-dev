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

**Public Alpha** — `init` / `scan` / `impact` / `validate` are stable today.

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
# Install
pip install codd-dev

# Initialize a new project
codd init --project-name "my-project" --language "typescript"

# Build the dependency graph from frontmatter
codd scan

# What breaks if I change this?
codd impact --diff HEAD~1
```

## 5-Minute Demo — See CoDD in Action

An LMS project. Write **requirements only**, let AI generate the rest, then change one thing and watch the impact ripple through.

### Step 1: Setup

```bash
pip install codd-dev
mkdir demo-lms && cd demo-lms && git init
codd init --project-name "demo-lms" --language "typescript"
```

### Step 2: Write requirements (the only human input)

Create `docs/requirements/requirements.md`:

```markdown
---
codd:
  node_id: "req:lms-requirements-v2.0"
  type: requirement
---
# LMS Requirements v2.0

## Functional Requirements
- Tenant management (organization-level isolation)
- User auth (email/Google OAuth)
- Course management (create/edit/publish)
- Progress tracking
- Reports & dashboards

## Constraints
- Tenant isolation via RLS
- Auth via Supabase Auth + Google OAuth
```

### Step 3: AI generates design docs

`codd generate` calls your AI CLI to produce design docs in Wave order. If `wave_config` doesn't exist, it auto-generates one from your requirements.

```bash
codd generate --wave 2   # System design + API design
codd generate --wave 3   # DB design + Auth design
codd generate --wave 4   # Test strategy
```

Each generated doc gets CoDD frontmatter automatically — `node_id`, `depends_on`, Wave number. No manual wiring.

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

7 docs, 15 dependency edges. Zero config files written by hand.

### Step 5: Change requirements mid-project

Add three lines to the requirements:

```markdown
## Additional Requirements (v2.1)
- SAML SSO (enterprise customers)
- Audit logging (record & export all operations)
- API rate limiting
```

Commit and run:

### Step 6: Impact analysis

```bash
codd impact --diff HEAD~1
```

```
Changed files: 1
  - docs/requirements/requirements.md → req:lms-requirements-v2.0

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

**3 lines changed → 6 out of 7 docs affected.** Green band auto-propagates. Amber band needs human review. Gray is informational. You know exactly what to fix before anything breaks.

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
  depends_on:
    - id: "design:system-design"
      relation: derives_from
    - id: "req:lms-requirements-v2.0"
      relation: implements
---
```

`graph.db` is a cache — regenerated on every `codd scan`.

## Commands

| Command | Status | Description |
|---------|--------|-------------|
| `codd init` | **Stable** | Initialize CoDD in any project |
| `codd scan` | **Stable** | Build dependency graph from frontmatter |
| `codd impact` | **Stable** | Change impact analysis (Green / Amber / Gray) |
| `codd validate` | **Alpha** | Frontmatter integrity & graph consistency check |
| `codd generate` | Experimental | Generate design docs in Wave order |
| `codd plan` | Experimental | Wave execution status |
| `codd verify` | Experimental | V-Model verification |
| `codd implement` | Experimental | Design-to-code generation |

## Claude Code Integration

CoDD ships with slash-command Skills for Claude Code. Combine with hooks for automatic coherence:

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

Every file edit triggers `codd scan` — the dependency graph stays current without thinking about it.

See [docs/claude-code-setup.md](docs/claude-code-setup.md) for complete setup.

## Comparison

|  | Spec Kit | OpenSpec | **CoDD** |
|--|----------|---------|----------|
| Spec-first generation | Yes | Yes | Yes |
| **Change propagation** | No | No | **Dependency graph + impact analysis** |
| **Derive test strategy** | No | No | **Automatic from architecture** |
| **V-Model verification** | No | No | **Unit → Integration → E2E** |
| **Impact analysis** | No | No | **`codd impact --diff HEAD~1`** |
| Harness-agnostic | Copilot focused | Multi-agent | **Any harness** |

## Real-World Usage

Dogfooded on a production LMS — 18 design docs connected by a dependency graph. All docs, code, and tests generated by AI following CoDD. When requirements changed mid-project, `codd impact` identified affected artifacts and AI fixed them automatically.

```
docs/
├── requirements/       # What to build (human input)
├── design/             # System design, API, DB, UI (6 files)
├── detailed_design/    # Module-level specs (4 files)
├── governance/         # ADRs (3 files)
├── plan/               # Implementation plan
├── test/               # Acceptance criteria, test strategy
├── operations/         # Runbooks
└── infra/              # Infrastructure design
```

## Roadmap

- [ ] Semantic dependency types (`requires`, `affects`, `verifies`, `implements`)
- [ ] `codd extract` — reverse-generate design docs from existing codebases (brownfield support)
- [ ] `codd verify` — full docs-code-tests coherence check
- [ ] Multi-harness integration examples (Claude Code, Copilot, Cursor)
- [ ] VS Code extension for impact visualization

## Articles

- [Zenn (Japanese): CoDD deep-dive](https://zenn.dev/shio_shoppaize/articles/shogun-codd-coherence)
- [dev.to (English): What Happens After "Spec First"](https://dev.to/yohey-w/codd-coherence-driven-development-what-happens-after-spec-first-514f)

## License

MIT
