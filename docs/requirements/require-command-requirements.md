---
codd:
  node_id: "req:require"
  type: requirements
  depends_on:
    - "req:extract-verify"
---

# Require — Requirements Inference from Code Facts

## Background

CoDD's V-Model philosophy: **intent lives only in requirements.** Everything below (architecture, detailed design, test) is structural fact extractable from code.

`codd extract` recovers structural facts. `codd restore` reconstructs design documents from those facts. But neither produces **requirements** — the "why" behind the code.

`codd require` closes the V-Model loop for brownfield projects by **inferring requirements from observed capabilities**. It answers: "given what this code does, what were the original requirements?"

### Epistemological constraints

This command operates under fundamental limits:

1. **Code reveals capability, not intent.** A rate limiter exists — but was it a requirement, or a developer's initiative?
2. **Bugs are indistinguishable from features.** Observed behavior = stated behavior.
3. **Absent features are invisible.** Requirements that were never implemented cannot be inferred.
4. **Business context is opaque.** Stakeholder trade-offs, regulatory motivations, and timeline pressures leave no trace in code.

These are not bugs to fix — they are the boundary of what static analysis can know. The command must make these limits explicit in its output.

## R1: Standalone CLI Command

### R1.1: Command signature

```
codd require [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--path` | str | `.` | Project root directory |
| `--output` | str | `docs/requirements/` | Output directory for generated requirements |
| `--scope` | str | `null` | Limit to specific service boundary (from `codd.yaml service_boundaries`) |
| `--ai-cmd` | str | `null` | Override AI command (defaults to `codd.yaml ai_command`) |
| `--force` | flag | false | Overwrite existing requirement files |
| `--feedback` | str | `null` | Review feedback from previous generation |

### R1.2: No wave config dependency

Unlike `generate` and `restore`, `require` must NOT require `wave_config` entries. It operates directly on extracted facts. This is critical for the brownfield bootstrap flow where no project configuration exists beyond the initial `codd extract`.

### R1.3: Prerequisites

- `codd extract` must have been run (extracted docs must exist in `codd/extracted/` or `docs/extracted/`)
- If no extracted docs found, error with: "Run 'codd extract' first"

## R2: Requirements Inference Pipeline

### R2.1: Clustering phase (deterministic, no AI)

Before invoking AI, group extracted facts by **service boundary**:

1. If `codd.yaml` defines `service_boundaries`: use them as clustering keys
2. If not: cluster by top-level directory under source_dirs (e.g., `src/services/auth/` → `auth`)
3. Always produce one **cross-cutting** cluster for system-wide concerns (auth patterns, error handling, logging, middleware)

Each cluster becomes one requirements document.

### R2.2: Inference phase (AI-assisted)

For each cluster, invoke the AI command with a prompt containing:

1. The extracted facts for that cluster (module docs, symbol lists, dependency graphs)
2. Cross-cutting extracted docs (system-context.md, architecture-overview.md) for context
3. Inference guidelines (see R3)

### R2.3: Output structure

```
docs/requirements/
├── system-requirements.md          # Cross-cutting: auth, error handling, infra
├── {boundary-name}-requirements.md # Per service boundary
└── ...
```

Each file must include CoDD YAML frontmatter:

```yaml
---
codd:
  node_id: "req:{boundary-name}"
  type: requirements
  depends_on: []
  confidence: 1.0          # Average inference confidence
  source: "codd-require"    # Machine-generated marker
---
```

## R3: Inference Quality Controls

### R3.1: Confidence classification

Every inferred requirement must be tagged with one of:

| Tag | Meaning | Criteria |
|-----|---------|----------|
| `[observed]` | Directly evidenced in code | Explicit route, exported function, DB table, test assertion |
| `[inferred]` | Reasonable inference from patterns | Code pattern suggests intent (e.g., retry logic → reliability requirement) |
| `[speculative]` | Weak evidence, needs human validation | Commented-out code, unused imports, naming conventions only |
| `[unknown]` | No evidence found — gap requiring investigation | Expected capability absent from extracted facts |
| `[contradictory]` | Conflicting evidence across modules | e.g., two auth strategies, inconsistent schema versions |

### R3.2: Section structure

Each requirements document must follow this structure:

1. **Overview** — What this service/boundary does (1-2 paragraphs)
2. **Functional Requirements** — Capabilities the code provides, each tagged with confidence
3. **Non-Functional Requirements** ��� Quality attributes inferred from code patterns
4. **Constraints** — Technology choices and architectural decisions observed
5. **Open Questions** — Ambiguities that need human clarification
6. **Human Review Issues** — Prioritized list of items requiring human judgment (contradictions, gaps, ambiguous intent)

### R3.3: Traceability

Every requirement must cite concrete source evidence:

```markdown
### FR-AUTH-01: Session-based authentication [observed]
Evidence: src/services/auth/session.ts:create_session() + tests/test_auth.py
The system uses session-based auth...
```

### R3.4: Anti-hallucination rules

The AI prompt must explicitly prohibit:

- Inventing features not evidenced in extracted facts
- Assuming standard features exist without code evidence (e.g., "the system probably has password reset" — only if code shows it)
- Generating aspirational requirements ("the system should...")
- Using generic boilerplate from similar systems

## R4: Human Review Integration

### R4.1: Review markers

Generated requirements include review prompts for humans:

```markdown
> **REVIEW NEEDED**: This requirement was [inferred] from retry patterns in the HTTP client.
> Is reliability an explicit project requirement, or incidental implementation?
```

### R4.2: Feedback loop

`codd require --feedback "..."` re-generates with human corrections incorporated. Same pattern as `codd generate --feedback`.

### R4.3: Promotion flow

After human review:
1. Human edits `docs/requirements/*.md` — resolves `[contradictory]` items, investigates `[unknown]` gaps, removes `[speculative]` items, confirms `[inferred]` → `[observed]`, adds missing context
2. `codd scan` picks up the reviewed requirements
3. Requirements become the source-of-truth for forward CoDD pipeline (generate → implement → verify)

## R5: Integration with Existing Commands

### R5.1: Pipeline position

```
codd extract → codd require → codd scan → codd generate → codd implement
     ↑              ↑              ↑
  structural    inferred        graph
   facts      requirements     update
```

`require` sits between `extract` and the forward pipeline. It bridges brownfield → greenfield.

### R5.2: Reuse from restore.py

The existing `_build_requirement_inference_header` and `INFERRED_REQUIREMENT_SECTIONS` in `restore.py` should be extracted into a shared module (or `require.py` imports from `restore.py`). Avoid duplication.

### R5.3: Extract output as input

`require` reads the same `ExtractedDocument` format that `restore` reads (via `_load_extracted_documents` from `planner.py`). No new data format needed.

## Non-Functional Requirements

### NFR-1: No new dependencies

`require` must not introduce new Python dependencies. It reuses the existing AI command interface and extracted document loading.

### NFR-2: Confidence cap

Machine-generated requirements must have `confidence ≤ 0.75` in frontmatter. They can never reach green band (0.90) without human review. This forces the human-in-the-loop.

### NFR-3: Idempotency

Running `codd require` twice without `--force` skips existing files (same behavior as `generate` and `restore`).

### NFR-4: Scoped execution

`--scope auth` generates only `auth-requirements.md`. Useful for iterative review of large codebases.
