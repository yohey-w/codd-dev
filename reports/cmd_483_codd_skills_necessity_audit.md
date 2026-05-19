# cmd_483: Bundled CoDD Skills Necessity Audit

Date: 2026-05-19  
Scope: `skills/{codd-init,codd-scan,codd-impact,codd-generate,codd-validate,codd-restore,codd-propagate,codd-assemble,codd-evolve}`  
Issue #25 handling: no inference about the reporter's intent. The issue is treated as a quality report: if skills are bundled, their `description` must be readable by host CLIs for natural-language auto-invocation.

## Executive Recommendation

Do not delete any bundled skill immediately in this command. The frontmatter repair from cmd_482 is still correct: all 9 bundled skills are now valid host-readable skill documents.

However, the product-facing skill surface should be narrowed:

- Keep as first-class host skills: `codd-evolve`, `codd-generate`, `codd-restore`, `codd-impact`, `codd-propagate`, `codd-init`.
- Demote or merge candidates: `codd-scan`, `codd-validate`, `codd-assemble`.
- Remove-now candidates: none.

Reason: `codd-scan` and `codd-validate` mostly wrap maintenance commands that setup hooks already automate. `codd-assemble` has real HITL/build value, but it is low-frequency, absent from the main README route, and should likely be folded into the greenfield generation journey rather than exposed as a separate default skill.

## Evidence Base

Files and commands inspected:

- `README.md:28-45`, `README.md:101-106`: primary product route is CLI-first; skills section names `codd skills` and highlights `codd-evolve`.
- `README_ja.md:28-45`, `README_ja.md:101-106`: Japanese README mirrors the same route and only highlights `codd-evolve`.
- `docs/claude-code-setup.md:25-48`: documents a slash-command skill table, but lists missing `codd-review` and omits existing `codd-assemble` and `codd-evolve`.
- `docs/claude-code-setup.md:68-88`, `docs/claude-code-setup.md:99-105`: `codd scan` and `codd validate` are designed to run through hooks, reducing the need for separate host skills.
- `docs/cli/skills.md:1-85`: `codd skills` docs focus examples on `codd-evolve`, not all 9 names.
- `pyproject.toml:79-83`: wheel packaging force-includes source `skills` into `codd/_skills`.
- `codd/skills_cli/discovery.py:13-39`, `codd/skills_cli/discovery.py:61-80`: install lookup order is explicit `--dir`, bundled `codd._skills`, then editable `skills/`.
- `codd/skills_cli/manager.py:64-83`, `codd/skills_cli/manager.py:85-106`, `codd/skills_cli/manager.py:108-136`: install/list/remove supports any discoverable bundled skill name.
- `codd/skills_cli/manager.py:260-283`: installed skill listing ignores `.bak.` entries and recognizes any directory containing `SKILL.md`.
- `codd/skills_cli/manager.py:310-315`: remove refuses non-CoDD-managed real directories, so deletion behavior is conservative.
- `tests/test_skills_cli.py`: tests currently exercise the skills manager primarily through `codd-evolve`; no test asserts the intended supported set of all 9 names.
- Current user install state from `python3 -m codd.cli skills list --target both --scope user --format json`: active CoDD bundled skill is only `codd-evolve` in both Claude and Codex user skill dirs. The other 8 bundled skills are not actively installed in this environment.
- Current git HEAD inspected: `e8e05a7f20e1bf48cdbaa6704e15dbac82f8be87` (`docs(reports): record cmd 482 skill frontmatter fix`).

## Issue #25 Interpretation

Issue #25 says 8 of 9 bundled skills lacked YAML frontmatter, which meant host CLIs could not read `description` for natural-language auto-invocation. cmd_482 fixed that quality defect:

- Frontmatter check on 2026-05-19: 9/9 PASS, SKIP=0.
- `python3 -m pytest tests/test_skills_cli.py`: 14 passed, SKIP=0.
- Fix commit: `dc634bb27f8ef36edc05a0b446110852cb74ee66`.
- Report/current inspected commit: `e8e05a7f20e1bf48cdbaa6704e15dbac82f8be87`.

This audit does not reverse that fix. Making descriptions readable was necessary for any bundled skill to be a correct host skill. The separate question is whether each skill should continue to be a first-class host-invoked skill.

## Judgment Table

| Skill | Primary journey | CLI overlap | Value beyond CLI | Current install signal | Recommendation |
| --- | --- | --- | --- | --- | --- |
| `codd-evolve` | Brownfield natural-language change | Uses many CLI commands | High: classification, stop-and-ask gates, requirements -> design -> lexicon -> source -> tests -> verify -> propagate -> runtime smoke chain | Installed in Claude and Codex user dirs | Keep, default flagship skill |
| `codd-generate` | Greenfield design generation | Wraps `generate`, `validate`, `scan` | High: wave model, one-wave-at-a-time control, HITL gates, guardrails | Not installed | Keep as advanced greenfield skill |
| `codd-restore` | Brownfield reverse engineering | Wraps extract/plan/restore/scan | High: inference limits, brownfield wave flow, HITL gates, guardrails | Not installed | Keep as advanced brownfield restoration skill |
| `codd-impact` | Diagnostic/change impact | Wraps `codd impact` | High: Green/Amber/Gray decision protocol and convention-alert handling | Not installed | Keep as diagnostic skill |
| `codd-propagate` | Code -> docs reverse propagation | Wraps `codd propagate` | High: analysis-first workflow, when-not-to-update rules, HITL gates | Not installed | Keep as diagnostic/maintenance skill |
| `codd-init` | First project setup | Close to `codd init` | Medium: detects existing CoDD state, guides language/requirements choice, branches to greenfield/brownfield next steps | Not installed | Keep, but do not over-promote |
| `codd-scan` | Graph refresh | Very close to `codd scan` | Medium-low: output interpretation and orphan/warning next actions, but setup hooks automate normal use | Not installed | Demote-to-docs or merge with `codd-validate` |
| `codd-validate` | Frontmatter/dependency check | Very close to `codd validate` | Low-medium: error triage exists, but pre-commit hook should run it routinely | Not installed | Demote-to-docs or merge with `codd-scan` |
| `codd-assemble` | Late greenfield assembly | Wraps `codd assemble` | Medium: build/HITL confirmation is useful, but route is low-frequency and not documented in README/setup table | Not installed | Merge into `codd-generate` finalization or demote until greenfield journey is productized |

## Minimum Skill Sets by Journey

### Greenfield

Minimum host skills:

- `codd-init`
- `codd-generate`

Optional:

- `codd-assemble` only if the user is actively using the generated-fragment assembly workflow.
- `codd-impact` for design change analysis after initial generation.

Do not require:

- `codd-scan` and `codd-validate` as host skills. Prefer hooks and direct CLI commands.

### Brownfield

Minimum host skills:

- `codd-init`
- `codd-restore`
- `codd-impact`
- `codd-propagate`
- `codd-evolve`

Rationale: brownfield needs both restoration and safe evolution. `codd-evolve` is the best natural-language entry point after the existing system is represented in CoDD artifacts.

### Diagnostic / Maintenance

Minimum host skills:

- `codd-impact`
- `codd-propagate`

Optional:

- `codd-evolve` when the diagnostic work turns into a functional change.

Do not require:

- `codd-scan` and `codd-validate` as separate skills; they should be hook/CLI maintenance actions.

## Compatibility and Packaging Impact

Because `pyproject.toml` force-includes `skills` into `codd/_skills`, deleting a source skill directory changes the wheel contents. Because `find_skill_source()` resolves bundled names from `codd._skills`, deleting a bundled skill makes `codd skills install <name>` fail for that name.

Impact by action:

- Keep all 9: no compatibility break. Requires only docs cleanup.
- Demote-to-docs while keeping directories: no `codd skills install <name>` break. Lowest-risk path.
- Merge with compatibility stubs: keep old directories for one release with descriptions pointing to the replacement workflow. Low product risk, slightly more maintenance.
- Hard remove: breaks existing `codd skills install codd-scan`, `codd skills install codd-validate`, or `codd skills install codd-assemble`. Also requires docs/tests updates and a release note.

Suggested migration wording if demoting:

```text
`codd-scan` and `codd-validate` are no longer recommended as default host skills. Use project hooks or direct CLI commands instead:

- `codd scan --path .`
- `codd validate --path .`

`codd-assemble` is now part of the greenfield finalization workflow. Use `codd assemble --path .` directly, or follow the finalization section in the greenfield generation guide.
```

## Implementation Scope if Karo/Lord Choose Demotion

Safe Phase 1, no public API removal:

- Update `docs/claude-code-setup.md`:
  - remove nonexistent `/codd-review`;
  - add existing `/codd-evolve`;
  - either add `/codd-assemble` as optional or explicitly mark it advanced;
  - mark `/codd-scan` and `/codd-validate` as hook-backed maintenance, not default slash commands.
- Update `docs/cli/skills.md`:
  - document the supported bundled names;
  - make `codd-evolve` the default recommended install;
  - show optional journey sets instead of implying all 9 should be installed.
- Add a test that asserts all bundled skill directories have `name` and `description` frontmatter and are discoverable by `find_skill_source()`.
- Do not delete source directories in this phase.

Phase 2, only after explicit product decision:

- Merge `codd-scan` + `codd-validate` into docs or a single maintenance skill.
- Fold `codd-assemble` into `codd-generate` finalization docs, or keep it as an advanced-only skill.
- If hard removal is selected, delete the chosen `skills/<name>` directories, update docs/tests, and include the migration wording above in release notes.

## Dashboard Decision Options

Recommended option for `dashboard.md` 要対応:

1. Adopt the narrow surface: keep 6 first-class skills, demote/merge `codd-scan`, `codd-validate`, `codd-assemble` without deleting directories yet.
2. Conservative option: keep all 9, but fix docs drift and explicitly say only `codd-evolve` is the default install.
3. Aggressive option: remove `codd-scan`, `codd-validate`, and `codd-assemble` from the bundled wheel now; accept `codd skills install <name>` breakage and publish migration wording.

## Residual Risks

- No built wheel artifact was unpacked in this audit. Packaging judgment is based on `pyproject.toml` force-include plus editable discovery checks.
- Host auto-invocation ranking behavior is host-dependent. This audit only verifies that descriptions are present and evaluates whether each description represents a useful invocation target.
- Current user install evidence is a single machine/session signal. It is useful but not telemetry.
- `docs/claude-code-setup.md` is materially stale: it mentions nonexistent `codd-review` and omits existing `codd-evolve`/`codd-assemble`.

## Validation

- `find skills -mindepth 2 -maxdepth 2 -name SKILL.md`: 9 skill docs found.
- `find_skill_source()` returned source paths for all 9 skill names in editable checkout.
- Frontmatter parser check: 9/9 PASS, SKIP=0.
- `python3 -m codd.cli skills list --target both --scope user --format json`: active bundled install is `codd-evolve` only.
- `python3 -m pytest tests/test_skills_cli.py`: 14 passed, SKIP=0.
- `git status --short --branch` before report creation: branch matched `origin/main`; unrelated untracked files `.codd/dag.json` and `docs/swebench/v2.11.0_run.md` already existed and were not touched.
