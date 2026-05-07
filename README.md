<p align="center">
  <strong>CoDD - Coherence-Driven Development</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/v/codd-dev?style=flat-square&color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/pyversions/codd-dev?style=flat-square" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License"></a>
  <a href="https://github.com/yohey-w/codd-dev/stargazers"><img src="https://img.shields.io/github/stars/yohey-w/codd-dev?style=flat-square" alt="Stars"></a>
</p>

<p align="center">
  <a href="README_ja.md">日本語</a> | English | <a href="README_zh.md">中文</a>
</p>

---

## North Star (Vision)

**"Write only functional requirements and constraints. Code is generated, repaired, and verified automatically."**

CoDD treats **requirements -> design -> implementation -> tests** as one DAG, mechanically verifies the coherence of every node, and lets an LLM repair inconsistencies automatically when they appear. Humans write only **what to build** and **where the boundaries are**.

## Where We Are (v1.34.0)

The North Star is far, but **within bounded conditions, CoDD has reached practical use**:

- ✅ Dogfooded on a real project (Next.js + Prisma + TypeScript Web app)
- ✅ `codd verify --auto-repair` completes with `PARTIAL_SUCCESS` on a real LMS project (attempts=4 / applied_patches=4 / unrepairable=2)
- ✅ DAG completeness with 9 coherence checks operational
- ⚠️ Single viewport / single persona assumed (Coverage Axis Layer C9 introduced in v1.32.0; axis variety is continuing work)
- ⚠️ Specification completeness Level 1 (finding holes in requirements) is planned for v1.35.0 `codd elicit`
- ⚠️ Other domains (Mobile / Desktop / CLI / Embedded / ML) are not yet validated
- ⚠️ Reducing unrepairable items is continuing improvement

```bash
pip install codd-dev
```

---

## Quick Start (5 min)

### 1. Install

```bash
pip install codd-dev
codd --version  # 1.34.0 or later
```

### 2. Add codd.yaml to your project

```yaml
# codd.yaml
codd_required_version: ">=1.34.0"

dag:
  design_docs:
    - "docs/design/**/*.md"
  implementations:
    - "src/**/*.{ts,tsx,py}"
  tests:
    - "tests/**/*.{spec,test}.{ts,tsx,py}"

repair:
  approval_mode: required   # automatic repair requires human approval
  max_attempts: 10

llm:
  ai_command: "claude"      # any LLM CLI can be invoked (claude / codex / gemini, etc.)
```

### 3. Common commands

```bash
# Coherence verification (checks consistency across requirements, design, implementation, and tests)
codd dag verify

# Verification with auto-repair (when violations are found, an LLM generates and applies patches)
codd dag verify --auto-repair --max-attempts 10

# Confirm User Journey PASS in a real browser (browser control via CDP)
codd dag run-journey login_to_dashboard --axis viewport=smartphone_se

# Derive implementation steps from design docs (input to the implementation phase)
codd implement run --task M1.2 --enable-typecheck-loop
```

### 4. Reading the output

`codd dag verify` runs 9 coherence checks:

| Check | Role |
|-------|------|
| `node_completeness` | Confirms nodes declared in design docs (implementation/test files) exist as physical files |
| `transitive_closure` | Confirms the dependency chain from requirements -> design -> implementation -> tests is closed |
| `verification_test_runtime` | Confirms tests for implementations can run and pass |
| `deployment_completeness` | Confirms the deployment chain (Dockerfile/compose/k8s) is complete |
| `proof_break_authority` | Confirms critical journeys are not broken |
| `screen_flow_edges` | Detects isolated nodes in the screen transition graph |
| `screen_flow_completeness` | Confirms every screen is mapped to requirements |
| `c8` | Detects uncommitted patches / dirty files |
| `c9` (`environment_coverage`) | Confirms **target environment coverage** such as viewport, RBAC role, and locale |

When violations are found, the deploy gate is blocked. With `--auto-repair`, CoDD enters a loop that asks an LLM to generate patches, applies them, and verifies again.

---

## Typical Use Cases

### Use Case 1: Automating requirements -> design -> implementation

Write "functional requirements + constraints" in `docs/requirements/*.md`, then run `codd implement run`:

1. An LLM dynamically derives ImplStep sequences from the requirements (Layer 1)
2. Best-practice gaps are filled in (Layer 2, such as logout, Remember Me, and session timeout for login)
3. After user approval through a HITL gate, implementation is generated into `src/**`
4. If a type checker such as `tsc` fails during generation, CoDD enters the auto-repair loop

### Use Case 2: Auto-Repair (`codd verify --auto-repair`)

Run `codd dag verify --auto-repair --max-attempts 10` in CI:

1. 9 coherence checks are executed
2. Violations are classified as **repairable (in-task) / pre-existing (baseline) / unrepairable** by a Hybrid Classifier (git diff + LLM)
3. Among repairable violations, the most upstream one in the DAG is selected and an LLM generates a patch
4. The patch goes through dry-run validation, then is applied and verified again
5. If all violations are resolved within `max_attempts`, the status is `SUCCESS`; if some are repaired, `PARTIAL_SUCCESS`; if only unrepairable items remain, `REPAIR_FAILED`

Even in `PARTIAL_SUCCESS`, repaired patches are kept and remaining violations are listed transparently in the report.

### Use Case 3: User Journey Coherence (`codd dag run-journey`)

Declare a user journey in the frontmatter of `docs/design/auth_design.md`:

```yaml
user_journeys:
  - name: login_to_dashboard
    criticality: critical
    steps:
      - { action: navigate, target: "/login" }
      - { action: fill, selector: "input[type=email]", value: "user@example.com" }
      - { action: click, selector: "button[type=submit]" }
      - { action: expect_url, value: "/dashboard" }
```

With `codd dag run-journey login_to_dashboard --axis viewport=smartphone_se`:

- `viewport=smartphone_se` (375x667), declared in `project_lexicon.yaml`, is injected into runtime through CDP
- The journey runs in a real browser (Edge / Chrome)
- If it fails, C9 `environment_coverage` in `codd dag verify` blocks the deploy gate

This structurally prevents incidents such as smartphone-only navigation disappearing unnoticed.

---

## v1.34.0 Key Features

| Feature | Role |
|---------|------|
| **DAG Completeness** (C1-C8) | 9 coherence checks across requirements, design, implementation, tests, and deployment |
| **Coverage Axis Layer** (C9) | Verifies **target environment coverage** such as viewport, RBAC role, and locale through a unified abstraction supporting 16+ axes |
| **LLM Auto-Repair (RepairLoop)** | Violation detection -> LLM patch generation -> apply -> verify again, attempting full resolution within `max_attempts` |
| **Hybrid Classifier** | Classifies violations as repairable / pre_existing / unrepairable using git diff (Stage 1) + LLM judgment (Stage 2) |
| **Primary Picker** | Prioritizes the most upstream violation in the DAG as the likely root cause among multiple violations |
| **PARTIAL_SUCCESS policy** | Returns PARTIAL_SUCCESS when applied_patches, pre_existing, or unrepairable items exist, avoiding release blockage by transparent non-current-task issues |
| **BestPracticeAugmenter** | Dynamically fills in best practices that are not explicitly written in design docs, such as password reset |
| **ImplStepDeriver (2-layer)** | Dynamically expands design docs into ImplStep sequences and infers `required_axes` in Layer 2 |
| **Typecheck Repair Loop** | Runs an auto-repair loop when a type checker such as `tsc --noEmit` fails during implementation |
| **`codd version --check --strict`** | Detects differences between the project's required CoDD version and the installed version |

See [CHANGELOG.md](CHANGELOG.md) for details.

---

## Case Study - A Real-World LMS Web App (Next.js + Prisma + PostgreSQL)

Result of running `codd verify --auto-repair --max-attempts 10` on a real-world LMS project (Web only, primarily single viewport):

```text
status:                   PARTIAL_SUCCESS
attempts:                 4
applied_patches:          4
pre_existing_violations:  1
unrepairable_violations:  2
remaining_violations:     3 (skipped + reported)
smoke proof:              6 checks PASS
CoDD core changes:        0 lines
```

Repaired files:

- `tests/e2e/environment-coverage.spec.ts`
- `tests/e2e/login.spec.ts`

Skipped violations (explicitly reported as outside CoDD's responsibility):

- pre_existing: deployment_completeness chain
- unrepairable: Dockerfile dry-run patch validation
- unrepairable: Vitest matcher runtime issue

C9 `environment_coverage` verified all axis x variant coverage for viewport (smartphone_se / desktop_1920) and RBAC role (central_admin / tenant_admin / learner), and reached PASS.

**Scope of this validation**:

- ✅ Auto-repair completes `PARTIAL_SUCCESS` on a Next.js + Prisma + TS stack
- ✅ Project-specific requirements are absorbed with **0 lines of CoDD core changes** (Generality preserved)
- ⚠️ Single project / single stack dogfooding only; other domains (Mobile / Desktop / CLI / Embedded / ML / Game) are not validated
- ⚠️ 2 unrepairable items remained = semi-automated, not fully automated

---

## Architecture - 4-Release Evolution and Next Plans

### Achieved (v1.31.0 - v1.34.0)

| Release | Milestone |
|---------|-----------|
| v1.31.0 | Inner 100% (internal coherence) - eliminated manual type fixes with the typecheck repair loop |
| v1.32.0 | Outer 100% (target environment coverage Layer C9) - absorbed viewport/RBAC/locale and related axes through a unified abstraction |
| v1.33.0 | Caveat-resolution path proven - real CDP run-journey + LLM auto-repair attempt passed |
| **v1.34.0** | **Full pipeline proven** - auto-repair reached PARTIAL_SUCCESS through dogfooding on a single Next.js Web project |

### Next (v1.35.0 - v2.0.0, Roadmap)

| Release | Plan |
|---------|------|
| **v1.35.0** | **`codd elicit`** - Discovery Engine that lets an LLM extract axis candidates and spec holes from requirements |
| v1.36.0 | BABOK lexicon (`@codd/lexicon/babok`) bundle + multi-formatter (md / json / PR comment) |
| v1.37.0 | **`codd diff`** - brownfield drift detection between requirements and implementation |
| v1.38.0 | extract -> diff -> elicit pipeline, complete brownfield flow |
| v1.39.0 | Reduce unrepairable items (RepairLoop strategy generalization) |
| v1.40.0 | Multi-domain dogfooding (Mobile / CLI / embedded, etc.) |
| (v2.0.0) | elicit + verify bidirectional loop, closest approach to the "fully automated" North Star |

See [CHANGELOG.md](CHANGELOG.md).

---

## North Star Connection: `codd elicit` (v1.35.0)

The biggest gap between the North Star ("write only requirements + constraints") and reality has been the assumption that **requirements are complete**. If requirements have holes, so does the implementation, and these surface as pre-demo incidents (e.g., navigation disappearing for central admin on a smartphone viewport).

`codd elicit` structurally addresses this:

```bash
$ codd elicit
[INFO] Reading docs/requirements/requirements.md (483 lines)
[INFO] Loading project_lexicon.yaml + @codd/lexicon/babok ...
[INFO] Generated 27 findings (axis_candidates: 11, spec_holes: 16)
[OK]   findings.md created
```

```markdown
## f-001 [axis_candidate] locale (severity: high)
**details**: variants: ja_JP, en_US / source: persona description and Section 3.5
**approved**: yes
**note**: en_US is phase 2

## f-002 [spec_hole] If the browser is closed during video playback, is progress lost? (severity: high)
**approved**: yes
```

```bash
$ codd elicit apply findings.md
[OK] project_lexicon.yaml updated (11 axis sections appended)
[OK] docs/requirements/requirements.md updated (TODO appended)
$ git add -A && git commit -m "feat: apply elicit findings"
```

Humans only **review extracted requirements** and **approve / reject elicit findings (Yes/No)**. The rest is AI dynamically diverging and converging.

---

## Generality Gate (Absolute Generality Preservation)

The following hardcoding is **forbidden** in CoDD core code:

- Specific stack names (Next.js / Django / Rails / FastAPI, etc.)
- Specific framework / library literals
- Specific domains (Web / Mobile / Desktop / CLI / Backend / Embedded)
- Specific viewport values (375 / 1920, etc.) or device names (iPhone / Android, etc.)
- Specific axis types (viewport / locale / a11y) or finding kinds (axis_candidate / spec_hole) hardcoded in core

All such knowledge is confined to **`project_lexicon.yaml` (project-specific)** or **lexicon plug-ins (`@codd/lexicon/babok`, etc.)**. CoDD handles them only as generic violation/finding objects.

When an LLM proposes a stack-specific optimal patch, that judgment is delegated to **the LLM's knowledge**. CoDD core does not decide it, which prevents overfitting.

---

## Hook Integration

CoDD ships hook recipes for editor and Git workflows:

- Claude Code `PostToolUse` hook recipe for running CoDD checks after file edits
- Git `pre-commit` hook recipe for blocking commits when coherence checks fail

Recipes live under `codd/hooks/recipes/`.

---

## License

MIT License - see [LICENSE](LICENSE).

## Links

- [CHANGELOG.md](CHANGELOG.md) - full release notes
- [GitHub Sponsors](https://github.com/sponsors/yohey-w) - support development
- [Issues](https://github.com/yohey-w/codd-dev/issues) - bug reports / feature requests

---

> When code changes, CoDD traces the impact, detects violations, and produces evidence for merge decisions.
