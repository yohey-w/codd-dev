<p align="center">
  <strong>CoDD — Coherence-Driven Development</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/v/codd-dev?style=flat-square&color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/pyversions/codd-dev?style=flat-square" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License"></a>
  <a href="https://github.com/yohey-w/codd-dev/stargazers"><img src="https://img.shields.io/github/stars/yohey-w/codd-dev?style=flat-square" alt="Stars"></a>
</p>

<p align="center">
  <a href="README_ja.md">日本語</a> | <strong>English</strong> | <a href="README_zh.md">中文</a>
</p>

<p align="center">
  <em>Write what you want. CoDD builds it from your requirements, keeps the docs and code in sync as things change, and runs the tests so they can't fake a "pass."</em>
</p>

---

## What is CoDD?

Picture a normal day on a real codebase:

- You change one function — and three other places that quietly depended on it break, because nobody remembered they were connected.
- The test suite goes green — but it never actually ran the code you just changed.
- The design doc still describes how the feature worked last month.

On a large project — or on code an AI wrote for you — this kind of drift is everywhere, and *"is everything still consistent?"* becomes impossible to answer by hand.

**CoDD turns that question into something a machine can answer.**

It builds a **map of how everything in your project connects** — which requirement is implemented by which code, which code is covered by which test, which config value switches which behavior. Once CoDD has that map, it can do three things for you:

1. **Build** — turn your requirements into design, code, and tests.
2. **Trace** — when you change anything, show everything the change affects, so nothing breaks silently.
3. **Verify** — run the real build and tests through a check that **refuses to report a fake pass**.

```mermaid
flowchart LR
    R["Requirements"] <--> D["Design"]
    D <--> C["Code"]
    C <--> T["Tests"]
    R -. "one connected map" .- T
```

The map works **both ways**: edit the code, and CoDD points to the design docs and requirements that are now out of date; add a requirement, and it points to the code and tests that need to change. That two-way consistency is the "Co" (coherence) in CoDD.

### How is this different from Copilot or Cursor?

Those tools make the *AI* smarter. CoDD makes the AI's *input* smarter. It hands the AI the exact map of what a change touches — with evidence for each connection — instead of letting it guess from whatever files happen to be open. And CoDD's verification is built so it **can't lie**: an empty test suite, a build script that's secretly just `true`, a missing test report — all come back **red**, never a quiet green.

---

## Install

```bash
pip install codd-dev          # needs Python 3.10+   ·   the command is `codd`
codd version                  # confirm the install
```

### Prerequisites

- **Python 3.10+**.
- **An AI CLI** — used by the commands that generate or update design, code, and tests (`greenfield`, `fix`, `generate`, `implement`, `brownfield`, …). The default is `claude --print`; override it with `--ai-cmd "<command>"` or `ai_command:` in `codd.yaml` (e.g. to use Codex CLI).
- **No AI needed** for the inspection commands (`init`, `scan`, `check`, `doctor`, `measure`, `impact`, `validate`, `plan` (show), `contract verify`, `dag verify`, `lexicon`, `version`, …) — they run as-is.

---

## Quickstart (60 seconds, no AI)

Drop CoDD into an existing repo, build the connection map, and run the health check. Everything here works without an AI CLI and is copy-paste-ready.

```bash
pip install codd-dev

cd path/to/your-repo
codd init myproject --language python   # create the CoDD config (codd/codd.yaml)
codd scan                                # build the "connection map" from your code
codd check                               # health check — start here
```

`codd check` is the front door: it runs `doctor` (config diagnostics) → `dag verify` (connection completeness) → `contract verify` (artifact contract) in one shot. Drill into details with `codd doctor`, or see metrics with `codd measure`.

From here, pick the entry point that matches where you are — build from scratch, recover design from existing code, or fix something already shipping.

---

## Three ways to start

### 1. Start a brand-new project — `codd greenfield`

Write what you want as a plain Markdown file, then let CoDD build the whole thing unattended (design → code → tests → verify, fixing problems as it goes):

```bash
codd greenfield --requirements docs/requirements/requirements.md \
                --project-name myapp --language python
```

It saves a checkpoint after every step, so `codd greenfield --resume` continues where it left off. Add `--dry-run` to preview the plan without calling the AI, or `--ntfy-topic <topic>` to get progress pings on your phone.

> **Language coverage today:** unattended greenfield is empirically validated end-to-end on all **six top languages — Python, TypeScript, JavaScript, Java, C++, and C#** — from the same neutral requirements spec (a multi-module calculator library, 15–20 files). Validation is execution-based: each run's verifiable behaviors are cross-checked against the language's native test report (pytest / vitest / surefire / ctest / dotnet-trx), and a run that does not converge is stopped honestly rather than reported as a false pass. Repetition is not uniform — TypeScript and Python have ≥2-of-3 independent green runs; JavaScript, Java, C++, and C# have n≥1. This validates the pipeline's cross-language wiring and convergence machinery at library scale; it does **not** yet claim enterprise-scale complexity (the subject of a follow-on real-spec campaign).

The same one-command pipeline (`codd greenfield --requirements FILE`) also ships as three transparent, adaptable vehicles you can read and tweak: a shell script ([`examples/greenfield_autopilot.sh`](examples/greenfield_autopilot.sh)), a Claude Code workflow ([`examples/claude_workflows/codd-greenfield.js`](examples/claude_workflows/codd-greenfield.js)), and a skill (`codd skills install codd-greenfield --target both`).

### 2. Work on an existing codebase — `codd init` + `codd scan`

CoDD reads your existing code, works out the design behind it, and keeps the two in sync from then on:

```bash
codd init                 # set CoDD up in your repo
codd scan                 # build the connection map from your code
codd brownfield .         # recover design docs, compare them to reality, list the gaps
```

### 3. Already shipping? Just describe the change — `codd fix`

```bash
codd fix "the login error messages are confusing"
```

CoDD finds the design docs your request touches, updates them, then carries the change through **design → code → tests → verify**. It only edits the files the map says are involved, and if the final check fails, it rolls back exactly those files — nothing else.

With no argument, `codd fix` is a legacy mode that picks up your failing tests or CI and repairs them (`codd fix --ci` / `--local` / `--dry-run`).

---

## How it works — three jobs, one map

| Job | What it does | Main commands |
| --- | --- | --- |
| **1. Build from intent** | Turns requirements into design options, then code and test scaffolding. The AI proposes; you choose (you stay in control). | `greenfield`, `plan`, `generate`, `implement`, `fix` |
| **2. Trace every change** *(the core)* | A connection map across requirements, design, code, config, data, and tests. Change one thing and CoDD shows the ripple — sorted into **Green** (safe to auto-fix), **Amber** (please review), **Gray** (just so you know) — with the reason for each link. | `scan`, `impact`, `propagate`, `diff` |
| **3. Verify for real** | Runs your actual build and tests so they can't fake a pass, and traces any failure back to the artifact that caused it. | `verify`, `check`, `coverage` |

These three feed each other in a loop: building decides *what* changes, tracing finds *where* it lands, verifying proves it holds — and every commit you make sharpens the map for next time. (Want the full story? See [`docs/explainer.md`](docs/explainer.md).)

---

## Command map

The most-used commands, grouped by purpose. Run `codd --help` for the complete list and `codd <command> --help` for the details of any one.

| Purpose | Commands |
| --- | --- |
| **Build** | `greenfield` (unattended) · `plan` (derive tasks) · `generate` (design/tests) · `implement` (code) · `assemble` (stitch fragments) · `fix` (from a phenomenon) |
| **Trace** | `scan` (update the map) · `impact` (ripple of a diff) · `propagate` (code → design) · `diff` (implementation vs requirements) · `watch` (file changes) · `drift` (URL/design drift) |
| **Verify** | `verify` (build + tests) · `check` (health — start here) · `doctor` (config diagnostics) · `dag` (completeness gate) · `contract` (artifact contract) · `coverage` (coverage gate) · `measure` (metrics) · `policy` (policy rules) · `validate` (frontmatter) |
| **Elicit & shape** | `elicit` (find spec gaps) · `lexicon` (industry checklists) · `brownfield` (recover from code) · `extract` / `require` / `restore` (facts → design) · `qc` (evaluate criteria) · `preflight` (task pre-check) |
| **Integrate & ship** | `mcp-server` (MCP) · `skills` (install skills) · `hooks` (Git hooks) · `deploy` (deploy) · `e2e` (E2E generation) |

---

## The Contract Kernel (introduced in v3.0)

Older CoDD had knowledge of specific languages and frameworks (Go, Python, Next.js…) baked into its core. **v3.0 took all of that out of the core** and moved it into swappable description files ("profiles") plus small adapters:

- **The core no longer knows any language or framework by name.** It just reads the profiles. So adding support for a new language or framework is a new profile + adapter — **no change to the core**. Bundled profiles: Python / TypeScript / JavaScript / Java / C++ / C# / Go.
- **Frameworks compose.** Next.js + TypeScript + Playwright + Prisma combine into one resolved description that `codd verify` runs against your project, live.
- **The "no fake green" rule belongs to the core.** A profile can adjust settings, but it can **never weaken** the anti-fake-pass guarantee. (Proven end-to-end on a real Next.js app, on the actual toolchain, with deliberate breakages that each correctly come back red.)

In short: one core now serves Next.js, Django, FastAPI, Rails, Go services, and more — and anyone can add support without touching it.

---

## Use it with your AI tools

- **MCP server** — `codd mcp-server` exposes CoDD to any MCP client (such as Claude Code or Cursor) over stdio. Configure it in Claude Code with:
  ```json
  "mcpServers": { "codd": { "command": "codd", "args": ["mcp-server"] } }
  ```
- **Skills for Claude Code & Codex CLI** — `codd skills install <name> --target both` drops ready-made skills (e.g. the greenfield autopilot) into `~/.claude/skills/` and `~/.agents/skills/`. Use `codd skills list` to see them and `codd skills remove <name>` to uninstall.
- **Codex App Server** — route AI calls through a persistent connection instead of a fresh subprocess each time (`codex_app_server.enabled: true` in `codd.yaml`), with automatic fallback.

---

## Hook Integration

CoDD ships ready-made hook recipes (under `codd/hooks/recipes/`) so coherence checks run automatically as you work:

- **Claude Code `PostToolUse` hook** (`claude_settings_example.json`) — runs CoDD checks right after each file edit.
- **Git `pre-commit` hook** (`git_pre_commit.sh`) — blocks a commit when it would break coherence. `codd hooks install` sets this up in one command.
- **Git `post-commit` hook** (`git_post_commit.sh`) and a **Codex CLI hook** (`codex_hook.sh`) — keep the connection map fresh as you commit.
- **A requirements-nudge recipe** (`claude_requirements_nudge.json`) — reminds you to re-run `codd greenfield --resume` when your requirements change (print-only; it never runs a pipeline on its own).

Copy the recipe you want from `codd/hooks/recipes/` into your editor or Git config to turn it on.

---

## Coverage lexicons

CoDD ships **39 ready-made "lexicons"** — checklists drawn from real industry standards — that you can switch on so `codd elicit` finds the gaps in your spec. They span Web (WCAG, OWASP, Web Vitals), Mobile (HIG, Material 3, MASVS), Backend (REST, GraphQL, gRPC), Data (SQL, JSON Schema), Ops (Kubernetes, Terraform, DORA), Compliance (ISO 27001, HIPAA, PCI DSS, GDPR, EU AI Act), and more. List them with `codd lexicon list` and enable one with `codd lexicon install <name>`. Turn on the ones that fit; add your own without touching the core.

---

## FAQ & troubleshooting

**Q. `codd: command not found`.**
A. pip's script directory isn't on your PATH. If you used `pip install --user`, add `~/.local/bin` to PATH. As a quick workaround, `python -m codd ...` does the same thing.

**Q. Which AI CLI do I need?**
A. The default is `claude --print`. Override it with `--ai-cmd "<command>"` or `ai_command:` in `codd.yaml` (e.g. Codex CLI). Commands like `init`, `scan`, `check`, `doctor`, and `measure` need no AI at all.

**Q. `greenfield` stopped or failed partway.**
A. Run `codd greenfield --resume` to continue from the last successful step (the checkpoint lives in `.codd/greenfield_session.yaml`).

**Q. `codd check` shows mostly SKIP or "vacuous."**
A. That's expected on a fresh project with no design docs or tests yet. As you run `scan` → `generate`/`implement`, the checks that read real data light up one by one.

**Q. Where does the config live?**
A. By default `codd/codd.yaml`. If your source directory is already `codd/`, use `codd init --config-dir .codd` to place it under `.codd/` instead.

**Q. `verify` fails even though everything looks green.**
A. That's anti-false-green at work. An empty test suite, a build that's secretly just `true`, or a missing test report is deliberately turned **red**. Check that your tests actually exercise the code.

**Q. How do I add a new language or framework?**
A. Add a profile (plus a small adapter) — the core stays untouched (that's the Contract Kernel). Bundled profiles: Python / TypeScript / JavaScript / Java / C++ / C# / Go.

**Q. How do I upgrade or uninstall?**
A. Upgrade with `pip install -U codd-dev`; uninstall with `pip uninstall codd-dev`.

---

## Documentation

- [`docs/explainer.en.md`](docs/explainer.en.md) — the full idea, from the connection map to AI-driven development ([日本語](docs/explainer.md) · [简体中文](docs/explainer.zh.md))
- [`docs/positioning.md`](docs/positioning.md) — where CoDD sits vs Spec Kit / coding agents / Copilot, on the false-green axis
- [`CHANGELOG.md`](CHANGELOG.md) — every release
- `codd --help` — the full command reference (in any project, `codd check` is the best place to start)
- [`docs/`](docs/) — architecture notes, setup guides, and a cookbook

---

## Contributing

Issues, pull requests, and lexicon proposals are all welcome — see [Issues](https://github.com/yohey-w/codd-dev/issues). CoDD is maintained by [@yohey-w](https://github.com/yohey-w), with thanks to everyone who reported the bugs and ideas that shaped it.

---

## License & links

MIT — see [LICENSE](LICENSE).

- [PyPI](https://pypi.org/project/codd-dev/)
- [GitHub Sponsors](https://github.com/sponsors/yohey-w) — support development
- [Issues](https://github.com/yohey-w/codd-dev/issues)
