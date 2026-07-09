# Changelog

All notable changes to CoDD (Coherence-Driven Development) are documented in
this file. CoDD is a Python developer tool published to PyPI as `codd-dev`
(command: `codd`).

The format follows [Keep a Changelog](https://keepachangelog.com/), and the
project aims to follow semantic versioning. Entries are most-recent-first.

Install or upgrade with:

```bash
pip install -U codd-dev
```

## [3.22.1] - 2026-07-09 — Revert the implement-oracle dependency-boundary gate (v3.22.0 Increment 1)

v3.22.0 Increment 1 (the implement-oracle dependency-boundary gate) is REVERTED in full. Its first
dogfood exposed an unsound foundation: it proved each generated source file's imports against the owning
design doc's transitive frontmatter-`depends_on` closure and FAILED any import landing outside it — but
frontmatter `depends_on` is an OPEN-WORLD ordering/context declaration (it drives producer-first
generation order and (B′) content injection), NOT a closed-world import allow-list. Design docs are
coarser than modules by construction, so a correct module import graph routinely exceeds the conceptual
doc closure. First TS dogfood on v3.22.0: 7/7 boundary findings were FALSE POSITIVES on textbook-correct
imports (e.g. `evaluator.ts` importing the AST module it evaluates), and the gate's own prescribed repair
(add a `depends_on` edge) lies OUTSIDE implement's write authority (source-only) — so the 5-attempt rerun
ladder was structurally unwinnable and a correct run hard-failed where v3.21.0 had passed.

Reverted (Fable5-authorized, self-critical): deleted `codd/dependency_boundary_coherence.py` + its tests;
excised the gate, its config knob (`implement.dependency_boundary_gate`), the rerun feedback block, and
both application sites from `codd/implement_oracle.py` (a tombstone NOTE marks the site);
`EVIDENCE_BOUNDARY_VIOLATION` stays a reserved vocabulary constant (back-compat; reserved for e2e/modality
breaches and any future design-declared governance adapter per rule 2a). A permanent reintroduction guard
(`tests/test_dependency_boundary_tombstone.py`) asserts the module, the gate functions, and the
`dependency_boundary` token are all absent.

**Increments 2 and 3 STAND** — the ts-v3 class that motivated Inc 1 (a generated structural test embedding
an invented layer map) is already killed generatively by rule (2a); real cross-module symbol/type
incoherence is judged by the native contract oracle + v3.21.0 producer-first/(B′) (this very dogfood
showed the rerun ladder converging type errors 58→21→1); `TEST_CONTRACT_OVERREACH` + repair design-context
remain.

Durable principles (Fable5, persisted to project memory so no future gate re-ships this class): **an
open-world declaration may STEER (ordering, context, prompts, diagnostics) but never JUDGE — only
closed-world contracts and native language-semantic oracles may gate**; and **a gate may only fire on
defects repairable within the firing phase's write authority.** The sound way to enforce topology is a
design-declared NEGATIVE constraint (a "must-not-depend-on" governance test via rule 2a) — recorded as
the future path, deliberately left unbuilt (zero demand). Full suite 7319 passed / 1 xfailed / 0 skipped.

## [3.22.0] - 2026-07-09 — Verify-stage structural/shape coherence (statically-typed ② verify unblocker)

After v3.21.0 (B′) unblocked the implement stage for statically-typed languages, both fresh TypeScript
greenfields advanced to and failed at VERIFY (2/2): test↔impl mismatches auto-repair cannot reconcile —
the class-1 meta-class at the VALUE/STRUCTURAL level a typechecker cannot catch. ts-v3: a generated
`dependency-boundary.test.ts` (an undeclared architecture test embedding an INVENTED layer map) that the
generated source violated. ts-v4: an AST-introspection test asserting node shape by string-keyed/`in`/
`toHaveProperty` access, whose property names the impl didn't match (typechecker-invisible). Python hid
both (dynamic typing tolerates shape/import drift at runtime). Fable5-authorized (owner delegated all
decisions to Fable5) three-increment fix, one canonical contract — the design DAG's declared
`depends_on` plus the (B′) producer artifacts — enforced at the two seams that already exist (the
implement oracle deterministically, the test-authoring prompt generatively); repair-side arbitration was
rejected as the primary path (least deterministic, latest in the pipeline). All F4 self-approvable
(Fable5-authorized; test immutability preserved as the anti-false-green core).

- **Increment 1 — implement-oracle dependency-conformance gate** (`codd/dependency_boundary_coherence.py`
  new; `implement_oracle.py`, `implement_oracle_types.py`). A language-free check, sibling to the
  orphan-artifact gate: every generated SOURCE file's internal imports must resolve into {its owning
  design doc} ∪ {that doc's transitive `depends_on` closure}; a resolved internal import to a doc
  PROVABLY outside the closure is a violation (frontmatter `depends_on` = data; suffix-map import
  extractors + shape-driven resolvers = shared machinery; zero language literals). Failure policy is the
  oracle's "provably-absent → fail; unknown → never fail" — unresolvable/undecidable degrade to logged
  residue. Violations normalize under the already-reserved `EVIDENCE_BOUNDARY_VIOLATION` category and
  feed the existing bounded rerun loop (default 5) via `build_contract_feedback` → `_invoke_rerun`, so
  the impl's imports are fixed BEFORE verify — the impl-side fix auto-repair never derived, done
  deterministically. Rerun feedback states the dual: import from a declared dependency, or it's a
  design-level gap (a missing `depends_on`) — do not inline/duplicate to dodge the boundary. Source-only
  v1 (test-tree imports excluded + logged); default-ON with an `implement.dependency_boundary_gate`
  opt-out.
- **Increment 2 — two test-authoring contract rules** (`implementer.py` `_spec_targets_tests` block).
  (2a) Structural/architecture-governance tests are DESIGN-GATED — a generated test must not read/glob/
  parse the source tree or assert internal module/layer/dependency structure unless the design declares
  such a governance test, and then its allowed-dependency data is derived verbatim from the design (no
  invented layer map); generalizes the shipped e2e-governance conditional. (2b) Shape assertions bind to
  declared shape — property/field names, key presence, discriminator strings asserted via
  typechecker-invisible means (string-keyed access, membership checks, property-name matchers,
  reflection) must be verbatim-traceable to the design's pinned surface or the (B′)-injected producer
  files; where a type system exists, prefer importing the producer's declared types and asserting
  through typed access so the compiler proves the binding (residual drift → a type error the oracle
  already catches). Extends class-1 pinnedness + the (B′) binding contract; keeps the non-weakening
  clause.
- **Increment 3 — repair honesty (arbitration-LITE)** (`codd/repair/design_context.py` new;
  `llm_repair_engine.py`, `loop.py`). Repair now receives the failing nodes' design-doc bodies
  (transitive closure, budget-capped) with the rule "tests are immutable; design pins + producer
  declarations are canonical; align the IMPL toward them." And a deterministic terminal reason
  `TEST_CONTRACT_OVERREACH` is emitted only when the failing assertion's surface tokens are provably
  absent from the design closure + producer files — a red-only LABEL on an already-RED terminal (no test
  edits, no patch-scope change, no green path), replacing the unhelpful
  `ALL_REMAINING_UNREPAIRABLE_OR_PRE_EXISTING` and pointing diagnosis at generation. Full arbitration
  (B-full) remains unbuilt.

Anti-false-green (Fable5): arbitration order is design-pinned surface / `depends_on` frontmatter →
producer-artifact declarations → unpinned (not structurally assertable). Every enforcement point moves
only the IMPL toward the canonical source, or blocks a test from asserting the unpinned; a genuinely
wrong impl still fails RED; a test that contradicts the design stays RED with `TEST_CONTRACT_OVERREACH`;
reconcile-to-a-wrong-test cannot occur. The `dependency-boundary.test.ts` overreach is fixed BOTH by not
generating it (2a) and by CoDD enforcing the invariant itself from the single source of truth
(Increment 1). Full suite 7322 passed / 1 xfailed / 0 skipped; generality ratchet green (no
`language ==` / per-language literal added). Behavioral change (new implement gate reds earlier on
silent boundary drift) → MINOR.

## [3.21.0] - 2026-07-09 — Cross-artifact symbol/type coherence (statically-typed ② unblocker)

The ② campaign cleared Python (3 unattended greens on v3.20.0) but TypeScript failed 2/2 at the
implement-time native-oracle typecheck: `independently-generated artifacts disagree on the
symbols/modules they import`. Root cause (Fable5-diagnosed): CoDD generates each source/test file
from its own design node in a separate model call, and NOTHING made those files agree on the shared
surface (public type/function/error names, signatures, module paths) before generation. Each call
independently spelled the same concept differently (AST type `Expr` vs `ExprNode`; tokenizer as a
class vs a function). Python's dynamic typing HID these disagreements (imports resolve lazily); a
static typechecker turns them into hard compile errors — so Python greened and TS did not. CoDD
actually HAD a cross-task symbol contract, but it was dead code (unwired when the sprint concept was
removed) whose extractors were JS/TS regex literals sitting in shared core (a latent generality
violation).

Fix (Fable5 (B′) — a self-approvable re-application of shipped machinery, no new gate/concept):
- **Producer-first ordering** — implement tasks now execute in topological order over the design
  DAG's `depends_on` closure (longest-chain rank, cycle-safe), source-kind before test-kind within a
  doc — the same wave semantics `generate` already applies to design docs, re-applied to implement
  units at the `list_implement_tasks` chokepoint. Ordering is a pure function of static DAG data, so
  a resumed run reproduces the identical order (no task skipped/repeated).
- **Dependency-artifact content as a binding import contract** — each implement task's prompt now
  carries the FULL on-disk content of its dependency design nodes' already-generated files (budget-
  capped, mirroring the existing own-output block; overflow degrades to name-level surface via the
  language-adapter seam, then paths-only). Contract wording is conditional: "when you import from
  these files, bind to their exported symbols, signatures, and module paths VERBATIM — do not
  re-declare, rename, or invent members." Full content (not symbol extraction) fixes signature/shape
  type-errors and contains zero per-language code by construction.
- **Anti-false-green discriminator (in the contract):** identifier spellings bind to the producer
  artifact (the first real declaration, itself generated under full design+lexicon context);
  behavioral requirements bind to the design; on conflict the design wins and the mismatch surfaces
  at the gate — never silently reconciled. The native-oracle gate is UNTOUCHED — it still proves
  agreement post-hoc; only what generation SEES beforehand changed.
- **Deleted the dead, language-hardcoded machinery** — `prior_task_outputs` plumbing, the "Prior
  implementations" prompt section, `_summarize_generated_task_output` (zero callers),
  `_format_prior_task_summary`, and the JS/TS `EXPORT_*_RE` regex literals in shared core (subsumed by
  the content injection; the language-zone adapter's own extractors are untouched). Shared core is
  now cleaner; the generality ratchet stays green.

Also folds in (committed post-3.20.0 without a CHANGELOG bullet): **honest terminal error on 0 files
from unparseable/empty AI output** — the `_zero_generated_files_error` message now carries the
per-attempt no-usable trail and drops the design-blaming `skip_generation` hint when every attempt
was empty/unparseable AI output (it was that ambiguity that caused a throttle event to be
mis-read as the class-2 doc-only variance). Append-only, terminal state byte-identical.

Full suite 7309 passed / 1 xfailed / 0 skipped. F4 self-approvable (re-application; no new
gate/concept/arbitration authority). Behavioral change (execution order) → MINOR.

## [3.20.0] - 2026-07-08 — ② mop-up: two implement-stage variance classes + verify-repair in non-git

Post-v3.19.0 hardening from the ② measurement endgame (repeated fresh unattended Python pure-library
greenfields on the RC). Of several identical-spec runs, some greened and some failed with distinct
LLM-generation variance classes; each was root-caused, consulted with the design authority (Fable5),
and fixed as a re-application of a shipped pattern (no new gate/concept category). Also folds in the
v3.19.0 review follow-ups.

- **Class 1 — test↔impl surface-form mismatch (generation-side, `codd/implementer.py`).** The model
  sometimes generated a test that bound an assertion to its impl's exact output surface form (a
  verbatim error-message substring, exact stdout bytes) that the design never pinned; the impl
  produced a different-but-reasonable form; the static implement-oracle passed; verify hard-failed on
  the assertion; and auto-repair correctly refused to rewrite a substantive test (anti-false-green) →
  recurring REPAIR_FAILED. Fix (Fable5): a sibling rule in the shipped "Scoped assertions" prompt
  block — bind an exact surface form ONLY when the design/dependency docs pin it (and then assert it
  verbatim); otherwise assert the semantic property the docs pin (the exception type + designed
  condition, the parsed value not its rendering, the presence of designed fields), never incidental
  wording/spelling/whitespace/formatting. Discriminator = design-pinnedness of the observable; no gate
  semantics change (a test that still over-binds fails exactly as today — no new green path). The
  repair-side arbitration widen was rejected as owner-gated.
- **Class 2 — doc-only design → 0-file implement hard-fail (deriver contract, `codd/llm/templates/
  plan_derive_meta.md`).** A doc-only detailed-design doc (a module dependency map / diagram, no code
  to author) got a code-demanding task and hard-failed at the implement 0-file gate; v3.16's no-op
  predicate keys on the derived task's output shape and could not see the source doc was diagram-only.
  Fix (Fable5, Fork B): one deriver-template rule — a design that authors documentation/diagrams only
  must declare its own document path as its sole `expected_outputs` — which routes into the SHIPPED
  v3.16 `_task_declares_no_authored_artifact` predicate (a `docs/**/*.md` path is a non-codebase
  artifact) so the task no-ops deterministically with a visible audit message. Zero new pipeline
  logic; the implement 0-file gate stays byte-identical (a real code design that produces nothing
  still hard-fails; empty outputs stay fail-closed; `skip_generation` stays HITL-only). Auto-marking
  skip_generation (Fork C) was rejected as a false-green laundering vector; duplicate-owner suppression
  (Fork A) was rejected (that shape dies earlier at the owner-uniqueness gate).
- **verify-repair in a non-git greenfield workspace (`codd/repair/git_patcher.py`).** The verify-stage
  auto-repair could not apply ANY patch in a greenfield workspace (not a git repo) because it
  hardcoded `git apply --3way` (requires a git object DB) → `'--3way' outside a repository`; a
  correctly-diagnosed fix bounced 10× and verify failed. Fix: apply `--3way` only inside a git
  worktree, else a plain `git apply`.
- **v3.19.0 review follow-ups** (Fable5 SHIP-AS-IS review + a second review): honor a pre-existing
  `deliverable.excluded_surfaces` on a forced re-plan (NB-1) + pin the intake residual (NB-2);
  brownfield reused-status honesty (removed a re-run false-green inside the anti-false-green feature);
  widen the language-free-core ratchet to negated/normalized/`match` dispatch shapes.

Anti-false-green preserved throughout: no gate weakened; both variance fixes are prompt/template
contracts routing into shipped machinery, touch no shared-core Python branch, and keep the generality
ratchet green. Full suite 7292 passed / 1 xfailed / 0 skipped.

## [3.19.0] - 2026-07-08 — Deliverable-surface fidelity (out-of-spec CLI exclusion)

**Closes the ② blocker where a "Pure library (no CLI)" spec still produces a CLI that fails verify.** A fresh
unattended Python greenfield on the v3.18.0 RC completed implement (the facade oracle passed), then FAILED at
`verify`: the deriver had designed a runnable console entry point (`src/<pkg>/__main__.py`) and a full e2e CLI
test suite despite the requirements saying "Pure library (no CLI, no I/O)" and "Out of scope: … a CLI". v3.18
greened the CLI's *import* (the facade resolves), but its e2e tests (`tests/e2e/test_cli_*.py`) fail behaviorally
and auto-repair exhausts at PARTIAL_SUCCESS — so v3.18 alone does not single-green a pure-library spec. Every
out-of-spec artifact is undesigned surface where variance breeds; v3.19 shrinks the deliverable surface to the
spec.

Fix (Fable5-designed, deterministic + default-permissive; the implement-oracle stays byte-identical):

- **Optional-surface vocabulary** (no new DSL): a `LayoutProfile` spec field `optional_surfaces: tuple[SurfaceSpec,
  …]` (`{id, description, paths}`) — a re-application of the `SourcePlacementSpec` (v3.13) / `facade_output_paths`
  (v3.18) profile-field pattern. The Python profile declares exactly one — `id="runnable-entrypoint"`, paths
  `(src/<pkg>/__main__.py,)`; every other profile declares none → strict no-op. Surface ids/descriptions are
  profile DATA; no surface semantics (and no `language ==`) enters shared core.
- **Plan-stage requirements intake** (bounded, deterministic, fail-safe): iff the resolved profile declares ≥1
  optional surface, one structured AI classification at the start of `_stage_plan` (before task derivation) reads
  the requirements text + the surface list and returns, per surface, `{excluded, evidence}`. A surface is excluded
  ONLY IF `excluded is true` AND `evidence` is a verbatim substring of the requirements (deterministic guard) —
  silence / ambiguity / parse-failure / a hallucinated quote all → NOT excluded (legacy). The decision is persisted
  to `codd.yaml` (`deliverable.excluded_surfaces`) so scaffold/derive/resume read a stable artifact and never
  re-classify; the key doubles as a manual override and kill-switch. Language-of-requirements-agnostic.
- **True-subtraction authority conditioning**: when a surface is excluded, its paths are subtracted from
  `harness_owned_scaffold_paths()` (so the scaffold does NOT create `__main__.py`, and a stray one is an honest
  ORPHAN — unlike v3.18's facade *content* carve-out, where the file is still created), the `_scaffold_python`
  write is conditioned in lockstep (authority parity: scaffold-created set == owned-scaffold set), and the layout
  prompt gains an "EXCLUDED DELIVERABLE SURFACE — do NOT author" rule.
- **Derive-stage surface fence**: a sibling of the v3.18 facade gate — any derived task whose `expected_outputs`
  intersect an excluded surface's paths triggers a bounded force-re-derive with deterministic feedback
  (`derive.deliverable_surface_max_retries`, default 2; 0 = legacy); exhaustion → `StageError` (honest RED).

Anti-false-green preserved: the fence only REJECTS (early-RED direction, never a new false-green); the intake is
default-permissive with a verbatim-evidence guard + a config kill-switch. Generality preserved: the vocabulary is
profile data, the 5 non-Python profiles are byte-identical (goldens), Python without an exclusion is byte-identical,
and no `language ==` entered shared core (grep + the new ratchet lint stay green). Version synced 3.18.0 → 3.19.0.

## [3.18.0] - 2026-07-08 — Package-facade ownership carve-out + derive-stage facade-coverage gate

**Closes the implement-oracle facade incoherence where a scaffolded-but-empty package facade fails the
post-implement gate.** On the fresh unattended Python greenfield (v3.17.0 RC), implement now completes ALL
derived tasks, then the implement-oracle hard-fails with `missing_symbol × 2` in `src/<pkg>/__main__.py`: the
generated module does `from <pkg> import ExprError, evaluate` but the package facade `<pkg>/__init__.py` is
EMPTY (only the scaffold docstring, no public-API re-exports). The oracle gate is CORRECT — this is a real
cross-artifact incoherence — so the fix is to make the facade authorable, not to weaken the gate.

Root cause (Fable5 diagnosis): the harness CLAIMS ownership of the facade file but never populates its
content, and TRIPLE-suppresses the AI content-author. (1) derive-strip: `exclude_harness_owned_outputs`
removes the facade from any derived task's `expected_outputs`. (2) contract exemption: the kind/completeness
checks impose no obligation on harness-owned declarations. (3) prompt prohibition:
`render_layout_placement_contract` tells the model "do NOT author or declare" the facade. Meanwhile the
scaffold writes only a docstring placeholder and never revisits it, and the composite oracle's repair feedback
steers away from populating the facade → non-convergence.

Fix (Fable5-designed, two INSEPARABLE components — the implement-oracle gate stays byte-identical):

- **Ownership carve-out** (facade topology = harness, CONTENT = AI). A new profile accessor
  `LayoutProfile.facade_output_paths()` names the package facade whose content the SUT authors, routed by the
  same legacy-bridge scaffolder id as `harness_owned_scaffold_paths` (STRICT no-op / empty for every other
  profile — no path prefix, no `language ==` literal). The module-level obligation authority
  `harness_owned_output_paths` now returns `harness_owned_scaffold_paths() − facade_output_paths()`, which in
  one change unblocks the derive-strip AND the kind/completeness contract; the layout-placement prompt
  subtracts the facade from its "do NOT author" list. The fence/orphan authority
  (`harness_owned_scaffold_paths`) is UNCHANGED — the scaffold still creates the facade and the orphan-gate /
  write-fence still exempt it. The un-stripped `expected_outputs` flow into v3.17.0's DECLARED DELIVERABLES
  prompt projection automatically.
- **Derive-stage facade-coverage gate** (exactly-one owner + bounded re-derive). At the top of the implement
  stage — before the scaffold and the per-task loop, and strictly before the owner-uniqueness hard gate — a
  deterministic set-math check counts the derived tasks declaring each facade path. Exactly 1 → OK; 0 or ≥2 →
  force a re-derivation (`_default_task_deriver` gains `force`/`feedback`) with a deterministic repair
  directive naming the path, re-approve, re-list, re-check. Bounded by a new knob
  `derive.api_facade_coverage_max_retries` (default 2; 0 = legacy immediate fail). Exhaustion → `StageError`
  (honest RED). The re-derive overwrites the cache so `--resume` stays consistent. An order rider stable-moves
  the facade-owner task to the END of the list (the aggregator authored after the modules whose symbols it
  re-exports). STRICT no-op for a stack with no facade (all 5 non-Python profiles) or an all-configured
  project.

Anti-false-green preserved: the implement-oracle is untouched; a facade that re-exports a NONEXISTENT symbol
stays RED (`__init__.py` is itself a scanned importer, so `missing_symbol` transposes onto it — regression-
guarded). Generality preserved: the carve-out is a strict SUBSET accessor keyed on the scaffolder id, empty
for every non-named-package stack; no `language ==` branch enters shared core (grep-gated). Side-finding
fixed: `codd/__init__.py` `__version__` was stale at 3.10.2 — now synced to the shipped version.

Deferred to a later increment: the deriver generated an out-of-spec CLI (`__main__.py`) despite requirements
saying "Pure library (no CLI)"; v3.18.0 GREENS it (the import resolves once the facade is populated), and the
no-CLI deliverable-surface fidelity is a separate increment.

## [3.17.0] - 2026-07-07 — Kind-contract envelope alignment + bounded feedback repair

**Closes the systematic implement halt where a bundled source+test task produced only its source.** On the
fresh unattended Python greenfield (v3.16.0 RC), implement failed — non-transiently, identically on resume —
with `task implement_tokenize_scanner: declared output kind(s) ['source', 'test'] but produced only
['source']`. The deriver bundled a source file and its test into ONE task; the model authored only the
source; the kind contract gate (`_verify_task_contract`) hard-failed a task the model could satisfy.

Root cause (deeper than "the retry loop has a hole"): a MIXED task's execution ENVELOPE is built source-pure
while only the gate judges by declared kind. `_test_only_output_paths` returns `None` for a non-test-only
task, so the output fence, the prompt frame (`is_test_related_implement` is False → no test-framework /
verifiable-behavior-contract sections), and the B2 test-root re-key all resolve source-only — the declared
test is dropped whether the model omits it OR writes it out-of-fence. Meanwhile the implementer's bounded
retry covers only the syntax gate and the 0-usable-files case; the kind contract is verified by the pipeline
AFTER the implementer returns, OUTSIDE that loop, so a partial produce (closer to success than 0 files) dies
with no feedback re-drive. Fresh runs re-derive (task shape varies — bundled dies, split greens); resume
replays the cached bundled task deterministically.

Fix (Fable5-designed, one design in three edits — the gate stays byte-identical):

- **Envelope alignment** (`_output_paths_for_task`): when a task's `_required_kinds` includes `test` but no
  resolved output path is under a configured test root, expose the test dirs too (excluding a `"."` root — a
  root-module language colocates tests). Gated on the DECLARED kind, so a pure-source task is never handed a
  test root; idempotent when the base already covers tests. This one change cascades: the fence accepts the
  test, B2 re-key activates, and `is_test_related_implement` flips True so the test-framework + VB-contract +
  anti-hollow-marker sections load from the first attempt.
- **Declared-deliverable projection** (`_build_implementation_prompt`): a "DECLARED DELIVERABLES" section
  lists the task's `expected_outputs` verbatim and requires ALL declared kinds — the outputs were previously
  shown only as existing-file context, never as the contract to fulfil. Explicitly forbids empty/skipped/
  assertion-free tests.
- **Bounded feedback repair** (`_default_implement_task_runner`): a bounded loop
  (`implement.kind_contract_max_retries`, default 2; 0 = legacy hard-fail) that, on a kind-contract miss,
  re-drives `implement_tasks` with feedback naming the missing kind + the verbatim declared outputs, and
  evaluates the contract by UNION across attempts (a re-drive that adds only the missing test satisfies it
  together with attempt 1's source, matching what is on disk). Budget exhausted → the gate itself raises the
  SAME hard StageError. This is the third application of the existing "hard gate + bounded feedback re-drive"
  pattern (attempt-level syntax gate, stage-level implement-oracle / VB-coverage, now task-level).

Anti-false-green preserved and strengthened: `_verify_task_contract` is unchanged (the loop only adds
attempts BEFORE it; the final round is the gate's own raise); union evaluation reflects the real files on
disk; the feedback restates the contract without hinting at relaxation and forbids hollow tests; and because
envelope alignment now loads the VB-contract + anti-hollow-marker sections into mixed-task prompts, hollow
pressure is REDUCED, not added. Every downstream gate (marker authenticity + tautology rejection, the
stage-wide VB coverage gate with its own source-fenced test rerun, verify's coverage-execution coherence,
the release suite) is untouched — the worst case is early-RED→late-RED, never a new false-GREEN. Green-path
cost is zero (the loop body runs only on a miss). Data-driven — no language / framework / task-name literal
in the pipeline core (grep-verified). New check/node/edge/concept: none. This is the RC increment for the
campaign's single-invocation unattended Python greenfield ② criterion.

## [3.16.0] - 2026-07-07 — Non-codebase-artifact implement no-op (generation-variance halt fix)

**Closes the stochastic implement-stage halt that blocked unattended Python greenfield.** A fresh
`codd greenfield` sometimes stopped at implement with `Design 'docs/infra/build_ci_setup.md' produced 0
generated files`. Root cause (not out-of-scope discarding): the planner legitimately plans an `docs/infra/`
build/CI design (`planner.py:49,977`), the implement deriver may turn it into a task, and the generation
prompt demands `concrete source files` unconditionally — so a run where the model correctly recognises "a
CI-description design authors no source" hard-fails, while a run where it fabricates `.github/workflows/ci.yml`
"succeeds" but usurps the **ci_scaffold stage's** deterministic ownership of that file. Same neutral spec,
run-to-run generation-variance.

Fix (Fable5-designed, single path): extend the EXISTING implement-runner predicate
`_task_declares_no_authored_artifact` — the seam set on 2026-07-03 for the release-gate false-RED ("a task
that authors nothing is an implement no-op, its substance owned by a later stage") — from "prose-only
declarations" to also cover **non-codebase artifacts**. A task whose every declared output is either a prose
gate declaration OR a multi-component path that is (a) not under any configured `scan.source_dirs`/`test_dirs`
root, (b) carries no implementation-language extension (the `LANGUAGE_EXT_MAP` registry DATA table; union of
all when the language is unresolved — fail-closed), (c) is not test-shaped, and (d) is not a glob → is a
**deterministic no-op**: no AI call, an echoed reason, and the owning stage provisions it later. This decides
from harness declaration DATA only — no doc name, node type, or `ci`/`github`/`infra`/language/framework
literal enters the logic (grep-verified).

Anti-false-green preserved and slightly strengthened:

- A no-op'd task runs NO model, so a generation failure **cannot** masquerade as 0-file success (there is no
  generation to fail). Every task that IS run keeps the 0-generated-files gate, the completeness/kind gates,
  and the write-time syntax gate byte-identical; `skip_generation` remains the sole HITL 0-file success.
- The "an implementation design is wrongly skipped" hole is closed structurally: a task bearing
  implementation declares a source/test-root or impl-extension output, so it never matches. Even a
  mis-derived module surfaces as a downstream RED (verify runs the real suite; the VB coverage/authenticity
  gates require declared behaviours to be covered; check) — the worst case is early-RED→late-RED, never a new
  false-GREEN. The authored/authenticated behaviour set is unchanged.
- It removes the fabrication pressure the old gate put on a genuinely code-less design (≥1 file or hard-fail),
  which is what let a run smuggle a hand-written `ci.yml` past the ci_scaffold deterministic owner.

Owner escape hatches unchanged: a configured `implement_targets`, any declaration under a source/test root,
and any impl-extension path (even misplaced) are ALWAYS generated; an EMPTY `expected_outputs` stays
fail-closed (absence of a contract is ambiguous, not a sanctioned skip). This is the RC increment for the
campaign's Python greenfield ② criterion (fresh, unattended, repeated green).

## [3.15.1] - 2026-07-07 — Environment/build failure defense (repair-thrash guard)

**Stops the repair engine from mutating known-good source while flailing at an environment failure it
cannot fix by editing code.** The Python greenfield dogfood (before the v3.15.0 third-surface fix landed)
showed exactly this: a `verification_test` node died `/bin/sh: python: not found` — an ENVIRONMENT failure —
yet the runtime failure carried no attribution, so the repairability classifier force-routed it and the LLM
"repair" thrashed real source (it deleted a CLI entry-point). v3.15.0's env-channel projection removes that
specific trigger, but the vulnerability CLASS (any env-shaped failure on the runtime/verification-template
surface — a missing tool, a refused connection, an un-provisioned dependency) remained. This is the
deterministic defense, built entirely on the EXISTING `environment_build_error` taxonomy — **no new
classification name, no new check/node/concept**:

- **D1 — attribute the runtime failure** (`verify_runner._failure_from_runtime_result`): the
  verification-template failure now runs through `attribute_command_failure` the same way the evidence path
  does, so it carries a `failure_class` / `code_addressable` instead of an unclassified dict. An
  unrecognized command (curl / cdp) → attribution `None` → details unchanged (byte-identical).
- **D2 — shell command-resolution signature** (`test_failure_attribution`): a `/bin/sh: <tool>: not found`
  (sh/bash/dash/zsh, with or without a leading path / `<lineno>:` prefix) is classed `environment_build_error`
  by a stack-agnostic adapter registered FIRST — so a `python -m pytest` that dies command-not-found is not
  mis-parsed by the pytest adapter as an `unknown` code failure. The signature is the shell's own
  vocabulary (`<tool>` is any name) — zero language/stack lexemes.
- **D3 — deterministic routing** (`repairability_classifier`): a violation classed `environment_build_error`
  is pulled to `unrepairable` BEFORE B0, the changed-files gate, and the LLM meta-classifier — the mirror of
  the B0 force-route. Applied in `RepairabilityClassifier` AND `NullClassifier` so even the no-LLM fallback
  can't hand the engine un-patchable infrastructure.
- **D4 — fence the state artifact** (`auto_scope_guard`): `.codd/verify/exec_env.json` (the recorded
  PATH-prepend dirs that steer verify spawn resolution) is added to the gate-control basenames, so the
  repair engine can never forge/edit it to redirect a spawn (belt to the `.codd/**` harness-owned
  suspenders).

Anti-false-green preserved: the observed test PASS/FAIL set is bit-unchanged (D1-D3 only add/steer
metadata; a genuine assertion failure with the shell-not-found signature ABSENT stays a code-addressable
`assertion_failure`). This is the RC build for the campaign's Python greenfield ② criterion.

## [3.15.0] - 2026-07-07 — Python test-execution environment provisioning (materialization's environment-continuation projection)

**Makes `codd greenfield --language python` runnable unattended on a python3-only host.** The Python
greenfield ExprCalc dogfood could not certify a build without a human because two root causes left the
generated project's tests un-runnable by the harness:

- **根因1** — the Python profile's verify argv is a bare `python` (`python.yaml` `commands.verify`/`test`),
  absent on a host that provides only `python3`, so the verify spawn died with a tool-missing error before
  any test ran.
- **根因2** — a generated CLI/subprocess-e2e helper invokes `[sys.executable, "-c", DRIVER]` assuming an
  **installed** package, but the pipeline never provisioned a venv nor ran `pip install -e .`. In-process
  pytest resolved the package via `pythonpath=["src"]`, but that path never propagates to a subprocess
  child — so the e2e child hit `ModuleNotFoundError`.

This is the **environment-continuation projection of toolchain materialization** — the last unfilled seat
of the greenfield "de-NO-OP the profile-driven gates" lineage (the same materialization→projection design
that wired npm `node_modules` (cwd-continuation), cmake `build/`, and the Java/C# in-tool resolvers).
Python is the first *environment*-channel stack: the interpreter identity IS the dependency store, so the
missing half was projecting the materialized environment onto the verify spawn.

Fix (4 parts; the shared core stays language-agnostic — zero `python`/`pip`/`venv` strings in
`verify_executor.py` or the shared `pipeline.py`; no `language ==` branch in core):

- **Provisioner realizer** (`project_types._provision_python_env`, Python zone): `sys.executable -m venv
  <project>/.venv` (never assumes a PATH `python`; a venv always yields `bin/python`), then a
  verifier-pinned pytest (`pytest>=8,<10`, the verifier's own toolchain — the SUT `pyproject.toml` is
  NEVER edited, mirroring the TS `_TYPESCRIPT_TOOLCHAIN_PROFILE` vitest pin) + `pip install -e .` INTO the
  venv. Idempotent probe (`import <canonical_package_name>, pytest`) short-circuits a usable env. Records a
  harness-owned state artifact (`.codd/verify/exec_env.json`: the real absolute bin dir + interpreter). A
  build failure is a code-NON-addressable `environment_build_error`, so the repair loop never thrashes on
  it.
- **Dispatch seam**: a 4th realizer field `env_provisioner: "venv-editable-pip-provisioner-v1"` on
  `python.yaml`'s `legacy_project_types` block, registered in `_ENV_PROVISIONERS_BY_REALIZER` (mirroring
  `_LAYOUT_BUILDERS_BY_REALIZER` / `_TEST_RUNNER_ENSURERS_BY_REALIZER`). The other 5 languages + Go declare
  no key ⇒ strict NO-OP, byte-identical.
- **Pipeline barrier**: `_stage_verify` calls the provisioner as a verify-direct StageError barrier (same
  tier as `_ensure_lock_freshness`), GREENFIELD-ONLY — a plain `codd verify` on a brownfield repo never
  grows a venv.
- **Exec-path prepend** (CORE, language-agnostic): `execute_verify_plan` gains an optional
  `exec_path_prepend: tuple[str, ...] = ()` that prepends existence-checked absolute dirs to the spawn's
  `PATH` before spawning; `verify_runner` reads the state artifact and passes the dirs (also threaded
  through the legacy `_run_evidence_command` path so resolution can't diverge). The unchanged
  `["python", ...]` argv now resolves to `.venv/bin/python`, whose `sys.executable` is the venv interpreter,
  so the generated e2e `[sys.executable, "-c", DRIVER]` child resolves the editable-installed package
  (cwd-independent — `sys.executable` is absolute). No state (brownfield / other langs / manual) ⇒ zero
  prepend ⇒ byte-identical.

Anti-false-green **strengthened**, not weakened: the venv is isolated (`--system-site-packages` is NOT
used), so a package present in the CoDD environment but undeclared by the project now honestly
`ModuleNotFoundError`s (the false-green a `sys.executable` + `PYTHONPATH` alternative would have
structurally embedded); the bare-basename src-layout import stays RED (`pythonpath` untouched); a forged
state artifact pointing at a non-existent dir is dropped by the existence check → `TOOL_MISSING` RED. All
observation gates (report-required / ZERO_TESTS / skip=red / SCOPE_MISSING) are untouched — the prepend
acts only on spawn resolution, never on classification.

### Third verify spawn surface — env-channel coverage completed (same version)

The dogfood that certified the four parts above surfaced a coverage gap the first cut's per-hand
enumeration missed: the exec-path prepend reached the contract executor (`execute_verify_plan`) and the
evidence command (`_run_evidence_command`) but NOT the **verification-template** surface. A
`verification_test` node runs its command through `template.execute(...)` (e.g. the `pytest_http` template's
`python -m pytest`), a third `subprocess.run` that inherited the ambient environment — so on a python3-only
host the bare `python` still died `/bin/sh: python: not found` after the venv was provisioned. Fix (shared
core stays language-agnostic — grep-clean of `python`/`pip`/`venv`):

- `VerificationTemplate.execute` gains a keyword `env: Mapping[str, str] | None = None` (default `None` ⇒
  inherit ambient, byte-identical). The RUNNER — never a template — reads the state artifact and builds the
  env; a template just forwards it to `subprocess.run`. The four shell templates (`pytest_http`, `vitest`,
  `curl`, `playwright`) forward it; `cdp_browser` accepts-and-ignores it (no bare `argv[0]` to resolve,
  same as its existing `cwd` handling).
- `verify_runner._verification_spawn_env()` builds the PATH-prepended env once from the shared
  `_exec_path_prepend()` (the SAME state artifact the other two surfaces use); a signature-checked shim
  (`_template_execute`) threads it, so an out-of-tree template on the legacy two-arg signature is called
  without `env` and keeps working.
- **Coverage is now a machine-checked invariant** (`tests/test_verify_observation_spawn_env.py`): an AST
  test asserts every `subprocess.run(...)` in the verify observation surface (runner, contract executor,
  and every verification template via glob) passes an explicit `env=` — a new template or spawn that
  forgets the env-channel turns RED before it can ship. This replaces the one-shot manual enumeration that
  let the third surface slip.

No state (brownfield / other languages / manual) ⇒ `env=None` ⇒ byte-identical spawn; the prepend acts only
on `argv[0]` resolution, never on the ZERO_TESTS / positive-execution gates.

## [3.14.0] - 2026-07-06 — C++ ctest→file execution attribution (completes the identity→file norm for all 6 languages)

**Verify-stage execution-attribution fix surfaced by the C++ greenfield ExprCalc dogfood.** After the
v3.13.0 header-placement fix let C++ implement pass, verify RED'd: `required test set 'tests' had zero
executed files in the report` (verify_executor.py SCOPE_MISSING). But the code is correct — `cmake --build`
+ `ctest --test-dir build` runs 43/43 GoogleTest cases green. Root cause: `CTestJunitReportAdapter`
returned empty `executed_*_files` by design, its docstring asserting ctest case names cannot be attributed
to `.cpp` files. That assertion was wrong. The module already codifies an "identity→file attribution norm"
(a runner that reports by non-path identifiers is resolved via a static identifier→file index built by
parsing the tree), implemented for Go (`_go_static_test_func_index`), Java (`_surefire_class_file_index`),
and C# (`_trx_cs_file_index`) — C++ was simply never done, so the last language's execution went
unattributed and the anti-false-green scope gate (correctly, given zero visible file execution) refused to
certify.

Fix (data-driven; no `language ==`; only the registered `ctest-junit` adapter changes):
- `_cpp_test_label_index(project_root)` — mirror of the Surefire index: `_iter_test_files` → filter via the
  EXISTING `CppTestBlockProfile` (the same gtest/Catch2 `TEST/TEST_F/TEST_P(Suite, Name)` parser the
  marker-authenticity gate already uses) → `{label → relfile}`, first-wins, unparseable = no contribution.
- `_ctest_case_label_candidates(name)` — normalizes gtest names (raw `Suite.Case`, the `Inst/Suite.Case/N`
  `TEST_P` shape, Catch2) to the static `f"{suite}.{name}"` label ctest's JUnit reports verbatim.
- `CTestJunitReportAdapter.parse` builds the index once and attributes each `<testcase>` to its file with
  the SAME pass/taint discipline as Surefire/TRX (a file is `executed_passed` iff ≥1 passed case and no
  fail/skip; a fail/skip taints it); an unattributed case is fail-closed (counted, credited to nothing).

Anti-false-green preserved: the index is location-only (no execution status) — a file is credited ONLY via
a real executed case in the report ∧ the static join, so a genuinely-unrun set attributes zero and stays
RED. Core (verify_executor / coverage_execution_coherence), cpp.yaml, the CMake scaffold, and the Go/Java/C#
adapters are byte-identical (their adapter tests unchanged and green). Options A (gtest native XML — collides
with `gtest_discover_tests`' per-case processes, dies on brownfield) and C (relax to executable granularity —
can't catch a compiled-but-zero-tests file) were rejected. Red-before-green tests added; full suite 7205
passed / 1 xfailed / 0 skipped. This completes the identity→file attribution family across all six languages.

## [3.13.0] - 2026-07-05 — Multi-source-set layout projection (C++ include/ headers + reference form)

**Third instance of the projection-class lineage (v3.11 import-coherence → v3.12 test-dir → v3.13
this), surfaced by the C++ greenfield ExprCalc dogfood.** The implement-time native oracle failed:
`src/test_harness/main.cpp` did `#include "ast.hpp"` but the headers were generated FLAT in `src/`,
while the harness-owned C++ layout puts headers under `include/{package}/` (the profile declares
`include/` three ways; the scaffold CMake sets the include path to `include/`). Root cause was not an
under-projection but a MIS-projection: v3.12's `render_layout_placement_contract` projected only a
SINGLE source_root (for C++, `package_root.kind=none` → `source_root == src`) and even told the model
"a file outside `src/` is dropped by the fence" — so flat headers in `src/` were the contract-COMPLIANT
output. The second source set (`include/`) was read nowhere; `LayoutProfile` had no field to carry it.

Fix (data-driven; no `language ==`; renders the multi-root rule ONLY for a stack that owns >1 source root):
- **`SourcePlacementSpec`** (`root`, `file_globs`, `reference_base`) + `LayoutProfile.source_placements`
  (default `()` → every legacy-built profile byte-identical by construction) + additive `to_dict` key.
- Populated ONLY by the generic synthesizer, one spec per declarative `source_sets` entry;
  `reference_base=True` iff the stack's first-party import rule is `include_path_prefix` with `base == root`
  (verified: only C++ matches — java=`source_root_package`, csharp=`root_namespace_prefix`, js/ts=`path_alias`).
- `render_layout_placement_contract` now emits, when `source_placements` collapses to >1 DISTINCT root, a
  multi-root SOURCE LOCATION rule (one bullet per owned root with its globs) plus a SOURCE REFERENCE FORM
  rule for each `reference_base` set ("a file at `<root>/<dir>/<name>.<ext>` is referenced as
  `<dir>/<name>.<ext>` from every other file, never by a bare same-directory filename"). Single-root stacks
  fall through to the v3.12 rule BYTE-FOR-BYTE. The reference-form rule is essential: placement alone would
  leave headers at `include/pkg/ast.hpp` still `#include "ast.hpp"`, recreating the identical oracle RED.

Option B (widen the CMake include path to `src/`) was rejected: it would make an empty `include/`
permanently green — an accidental-green masking a real layout incoherence for every future C++ run. The
fix is prompt-side projection only — zero new enforced gate, no new false-red surface. Golden byte-equality
tests lock python/js/ts/java/csharp renders unchanged; red-before-green for the C++ multi-root + reference
case; full suite 7195 passed / 1 xfailed / 0 skipped. Recovery of a failed run is a fresh generate (not
`--resume`: stale `src/*.hpp` keeps satisfying same-dir includes and would mask the incoherence).

## [3.12.0] - 2026-07-04 — Layout-placement projection + deterministic test-root re-key (greenfield test-dir freelancing)

**Projection-class fix surfaced by the JS greenfield ExprCalc dogfood.** Implement hard-failed at a
task declaring a `test` output (`test/errors.test.js`): the harness owns a single test root `tests/`,
but the generation prompt never conveyed WHERE tests live, so the model freelanced the sibling `test/`
(unit specs) — the output-path fence dropped the misplaced file, its declared `test` deliverable read
as "not produced", and the declared-output-kind check hard-failed the stage. Confirmed in source: the
generator names `tests/e2e/` explicitly (so the model obeyed there) but never names the unit test root,
so only the unspecified part was fabricated — direct evidence that projecting the layout fixes it.
Two independent gaps, one fix (A + B), both data-driven (no `language ==`; read from `LayoutProfile`):

- **A — layout-placement contract projection.** New language-free `render_layout_placement_contract(profile)`
  (project_types.py) projects the harness-owned TEST ROOT (all test files + referenced test paths under
  `{test_root}/`; don't invent a sibling `test/`/`spec/` — the forbidden-sibling example is *derived* as the
  common names minus the owned root, so a brownfield stack whose root IS `test` is never told to avoid it),
  the SOURCE ROOT (only when `not requires_package_init`, so it doesn't duplicate the Python import contract),
  and "root tool/runner config files are harness-owned — don't author or declare them". Injected into BOTH
  the generate-stage prompt (so design docs stop fabricating `test/`) and the implement-stage prompt (beside
  the existing import contract). `profile is None` → `""`. This is the same same-truth-source seam as
  `render_import_coherence_contract` / `render_vb_contract` / `resolve_test_framework_guidance`, and is where
  a TypeScript build/emit contract will later be projected.
- **B — deterministic re-key to the owned test root (NOT a fence relaxation).** B1: at task-load
  (`list_implement_tasks`), a test-shaped declared output under NONE of the configured scan roots is
  replanted under `test_dirs[0]` (`test/errors.test.js` → `tests/errors.test.js`), normalized on READ
  (the `.codd/derived_tasks` cache is never mutated, so every consumer incl. `--resume` agrees on one path);
  whole no-op when `test_dirs` is empty or any root is `.` (protects root-module stacks like Go). B2: a
  fence-side backstop in `_parse_file_payloads`' drop branch re-keys a still-misplaced test-shaped payload
  under the owned root (collision-guarded; replaces the drop only, never touches in-prefix files). Relaxing
  the fence to accept `test/` in place would be a false-green — `_produced_kinds` counts a test-shaped file
  as kind `test` wherever it sits, so only re-keying it to where the verify runner actually runs is correct.

Refactor: `_has_test_shape` and friends moved to `operational_e2e_audit.py` (leaf module) to let both
pipeline and implementer reuse them without an import cycle. Red-before-green tests added (33: renderer,
B1, B2, and an ExprCalc-shaped greenfield integration case); full suite 7185 passed / 1 xfailed / 0 skipped.
Recovery of the failed run: `codd greenfield --resume` (zero generate cycles) — B1 re-keys the declared
output, A conveys the contract, B2 backstops; the stale `test/` refs left in the design docs are inert to
every gate (VB audit is id-driven; verify walks the on-disk `test_dirs`).

## [3.11.0] - 2026-07-04 — Import-coherence false-RED: ambient stdlib-shadow exemption + confirmed-flow dynamic-import collection

**Anti-false-RED fix to the verify-stage import-coherence gate** (`codd/import_coherence.py`),
surfaced by the ExprCalc Python greenfield dogfood: the gate hard-failed a fully coherent
source+test set with 7 `bare_basename_import` findings, forcing a spurious full regeneration.
Two independent root causes, both fixed data-driven (no `language ==` branch; the false-RED
class is not project-specific — `types` / `json` / `errors` / `logging` are common first-party
module names and structural-guard tests that hold module names as DATA are a common pattern):

- **Ambient stdlib-shadow exemption (A′).** A first-party source module whose bare name
  collides with a runtime/stdlib module (a domain `ast.py` shadowing Python's stdlib `ast`) is
  now EXEMPT from the bare-import check: a bare `import ast` in a test cannot be CONFIRMED as a
  first-party reference (in every normal environment it resolves to the ambient module), so
  flagging it violates anti-false-red. Driven by a new opt-in `LayoutProfile.ambient_modules`
  sentinel (`"python-stdlib"` for Python) resolved at runtime from the live interpreter
  (`sys.stdlib_module_names | sys.builtin_module_names`) — the core hardcodes no module list,
  and a stack declaring no sentinel is unchanged (`None` default, opt-in). The exemption is
  recorded in the gate's detail line.
- **Confirmed-flow dynamic-import collection (B′).** A module-name string literal is collected
  as a dynamic-import reference ONLY from a confirmed flow — the literal argument of an
  `importlib.import_module(...)` / `__import__(...)` call, or a Name bound to literal values (the
  tuple-iterated `for m in (...): import_module(m)` pattern) — instead of ANY single-segment
  string anywhere in a file that merely uses `importlib`. This removes the false-RED where a
  structural/graph test resolves modules package-absolutely via an f-string `import_module` call
  and ALSO holds module names as assertion DATA. The codex3 bare tuple-iterated pattern stays a
  true-positive RED (the Name is resolved to its literal loop elements).

Detection power preserved: a real bare `import <mod>` / `import_module("<mod>")` / `__import__`
of a non-ambient first-party module is still flagged; missed cases fall through to pytest as an
honest `ModuleNotFoundError` in the harness-owned verify env, never a false-green. Red-before-green
tests added for every case (generic names, `tmp_path`); full suite 7152 passed / 1 xfailed / 0
skipped; the ExprCalc dogfood goes 7 findings → 0. Follow-up (separate commit): also run
`check_import_coherence` at implement-end so a genuine violation feeds the existing re-implement
feedback loop while the SUT can still edit files (the verify-stage hard gate stays the backstop).

## [3.10.2] - 2026-07-03 — Verification/gate task false-RED: a prose-output task authors nothing

After the scaffold + all 19 module/test implement tasks passed, the Python
ExprCalc greenfield hard-failed implement on `run_full_pytest_release_gate`:
`Design 'docs/infra/test_runner_setup.md' produced 0 generated files`. That task
is a VERIFICATION/RELEASE-GATE unit — its `expected_outputs` are prose
(`pytest -q output`, `pytest tests/e2e -q output`), not files — so the
implementer honestly emits 0 files and the 0-generated-files gate rejects it
(after burning 3 no-usable retries). Its real work (install + run the suite
green, SKIP=0) is exactly what the **verify** stage already performs.

### Fixed
- **A verification/gate task that declares no authored artifact is a no-op in
  implement** (deferred to verify). The greenfield implement runner now
  recognizes a task whose every declared output is a PROSE description (contains
  whitespace and is not a file path, glob, or path under a configured
  source/test root) and returns a clean no-op WITHOUT invoking the implementer,
  instead of demanding generation. No gate is weakened: verify re-runs the full
  suite as the release gate, any task declaring a real path-shaped artifact stays
  fully gated (0-files + completeness), and an empty `expected_outputs` stays
  strict (fail-closed). Also saves the wasted derive-steps + AI generation the
  honest 0-file miss would burn. Generic, language-independent (no `language ==`).
  New helper `_task_declares_no_authored_artifact`.

## [3.10.1] - 2026-07-03 — Scaffold-task false-RED: a bare directory output imposes no author-kind

The Python ExprCalc greenfield autopilot hard-failed at implement on
`scaffold_package_and_pyproject` with `declared output kind(s) ['source','test']
but produced only ['source'] (missing ['test'])`. The task legitimately creates
a package skeleton plus **empty** `tests/` + `tests/e2e/` directories ("populated
later") and authors no test — but the implement kind gate classified those two
bare-directory declarations as TEST deliverables and demanded a produced test
file. This is the same false-RED CLASS previously seen for the Java/C++/C#
scaffold tasks, at the directory-declaration level.

### Fixed
- **A bare DIRECTORY `expected_outputs` entry carries no deliverable-KIND
  obligation.** `_classify_declared_output` now returns UNKNOWN for a directory
  declaration (`tests/`, `tests/e2e/`, `src/pkg/`) — it is structural scaffold
  intent created by `mkdir`, never an authored artifact — unifying the kind gate
  with the completeness gate, which already leaves directory declarations
  unchecked. The two gates now share one truth: only a concrete FILE path or a
  glob carries an obligation. Anti-false-green is fully preserved: a real test
  task declares a test FILE (`tests/test_x.py`) or a test-name GLOB
  (`internal/httpapi/*_test.go`), both still classified TEST and still gated, so a
  test task that emits only source still hard-fails. Generic (no `language ==`;
  language-independent path classification). New helper `_is_bare_directory_decl`.

## [3.9.0] - 2026-07-03 — VB contract projection + bounded authenticity rework (Top-6 greenfield unblock)

Python/JavaScript/TypeScript greenfield autopilots all stalled at the FINAL
anti-false-green gates: the implement prompt stated the marker RULES but never
enumerated the CLOSED set of declared verifiable-behavior ids, so the model
invented ids (acceptance-criterion ids like `AC-10` leaking into `vb=` markers,
descriptive inventions like `VB-TOK-NONZERO-POSITION`), and the marker-
authenticity gate — unlike the coverage gate — failed CLOSED on the first non-
credible marker with no way to feed the finding back. The ExprCalc Python
dogfood stalled with 24 markers (17 orphans + 7 assertion-less). Every fix is
generic (no `language ==` in core; per-language idioms live in profile YAML).

### Added
- **VB contract projection.** `verifiable_behavior_audit.collect_declared_vb_ids`
  (behavior-invariant re-exposure of the gate's own declared set) +
  `render_vb_contract` (a language-free prompt block enumerating the closed id
  list + assertion-quality + coverage-is-completion rules), injected into the
  implement prompt for test-scope tasks from the SAME truth source the gate
  reconciles against — the prompt-side closed list can no longer drift from the
  gate-side declared set. Opt-in per-language assertion idioms ride a new
  `tests.assertion_guidance` profile field.
- **Bounded authenticity rework loop** (`greenfield.vb_rework.max_rounds`,
  default 2, 0 = legacy). On an authenticity failure the owning TEST tasks are
  re-driven with the verbatim findings + the closed VB contract, then the
  UNCHANGED gate re-judges. Guards: an oscillation abort (finding count must
  strictly shrink round-over-round), a VB-table tampering RED (the declared id
  set must not change during rework), and a coverage-regression RED.
- **Self-comparison tautology detection** (`tautology_direct`): `assert x == x`
  / `assertEqual(sorted(v), sorted(v))` are rejected via normalized-operand
  equivalence on vacuously-true operators only — the NaN idiom `x != x` stays
  valid (anti-false-red).
- **Marker distribution report** (`summarize_marker_distribution`) — visibility
  into marker stacking, NOT a cap (a table-driven test may cover several VBs).

### Changed
- The verify-stage repair prompt now carries the VB contract when a test file is
  among the failing artifacts, so a repair that edits a test cannot introduce an
  orphan marker or a constant/self-comparison assertion.

The deterministic gate is never loosened — this only adds detections and grants
the model bounded, gate-judged retries.

## [3.7.6] - 2026-06-25 — brownfield discovery hardening (generality-first; 6-language stress dogfood)

A second, more complex OSS per language (SQLAlchemy, Fastify, NestJS, Guava, LevelDB,
Newtonsoft) showed CoDD's import RESOLVERS hold at scale, but the brownfield DISCOVERY layer
silently under-covered real-world layouts. Every fix here is generic (no per-language or
per-project special-casing).

### Added
- **Discovery completeness accounting.** After a DAG build, CoDD warns when on-disk source
  files exceed graph nodes (naming the uncovered files) and when an internal-looking import
  specifier resolves to nothing (an "unresolved residue" count) — silent under-coverage
  becomes visible instead of passing quietly.

### Fixed
- **Source discovery dropped real files across many layouts.** Auto-detection now covers
  root-level source files alongside subpackages, packages whose source lives only in a
  nested subdirectory, C++ trees scoped to `include/` only, and — for FQN/namespace
  languages — a scan rooted *inside* the package tree (which previously double-prefixed the
  path into a silently empty graph). One generic mechanism (e.g. SQLAlchemy 255→444 nodes; a
  Java tree scoped inside its package 0→2015 edges).
- **Divergent C++ include resolvers unified.** Builder and scanner now share one
  include-resolution path so they can't drift (a non-`include/` layout went 38%→93% resolved).
- **BOM-prefixed first lines** (e.g. a UTF-8 BOM glued to a `namespace`/`import` on line 1)
  are handled for every language.
- **Java package-implicit edges** no longer fan out to every sibling — only real import
  edges are emitted (Guava 427k spurious → 6.9k real, also removing an O(n²) blow-up). Import
  edges are labeled by actual kind; the web-only seed heuristic no longer injects into
  non-web projects.

## [3.7.5] - 2026-06-25 — TypeScript fix + C# brownfield support (top-6 language coverage)

With this release CoDD builds real dependency graphs on brownfield projects across the six
most common languages (Python, JavaScript, TypeScript, Java, C++, C#) — each verified on a
famous OSS that was not built with CoDD.

### Added
- **C# structural support.** CoDD recognizes C# (`.cs`), detects the language (fixing a
  false green where C# repositories were mis-detected as Python and `codd check` passed
  having verified nothing), and builds `using`-based dependency edges through a
  namespace→declaring-files reverse index — C# namespaces are not directory-tied, so a
  `using Dapper.X` resolves to every file declaring `namespace Dapper.X` (exact match, with
  an out-edge cap as an explosion guard). (Dapper: mis-detected Python / 0 edges → C# /
  122 real nodes / 11 using + 54 tested_by edges.) Regex-based; tree-sitter-c-sharp and the
  oracle/profile are deferred.

### Fixed
- **TypeScript dependency graphs were silently empty under modern TS-ESM.** Import
  resolution did exact-match + suffix-append only, so specifiers carrying the mandatory
  `.js` extension that resolve to `.ts` sources (`import { x } from "./types.js"` →
  `types.ts`, the NodeNext/Bundler convention) produced zero edges. Resolution now falls
  back — only after an exact match fails, so extensionless JavaScript is unaffected — to
  swapping emitted ESM extensions (`.js/.jsx/.mjs/.cjs`) to their source counterparts
  (`.ts/.tsx/.mts/.cts`), in both file and directory-index form. (Zod v4: 0 → 356 import
  edges.)

## [3.7.4] - 2026-06-25 — C++ brownfield support (#include edges, false-green fix)

### Added
- **C++ structural support.** CoDD now recognizes C/C++ (`.c/.cc/.cpp/.cxx/.h/.hpp/.hh`),
  detects the language and `include/` source roots, and builds `#include` edges — a local
  `#include "fmt/core.h"` resolves PATH-based (relative to the including file, then the
  include roots) into impl→impl edges, while `<system>` includes are external.
  Reachability / transitive-closure now work on C++. (fmt: mis-detected as Python with 0
  edges → detected as C++ with 67 real nodes and 36 #include edges.) Regex-based;
  tree-sitter-cpp wiring is a deferred enhancement.

### Fixed
- **False green on C++ projects.** Because C++ extensions were absent from language
  detection, a C++ project was mis-detected as Python, its code was invisible, and
  `codd check` PASSed having verified nothing. With C++ now visible and its graph built,
  the checks analyze the real code instead of passing vacuously.

## [3.7.3] - 2026-06-25 — Java brownfield support (import edges, tree-sitter, proto-enum crash fix)

### Added
- **Java structural support.** The DAG now builds Java import edges — `import com.x.Y;`,
  `import static`, wildcard `import com.x.*;`, and package declarations are extracted and
  resolved against JVM source roots (`src/main/java`, `src/test/java`) into impl→impl
  edges, so reachability / transitive-closure work on Java for the first time.
  tree-sitter-java is wired into the parser registry (Java auto-promotes from the regex
  backend to tree-sitter); the regex backend also gains Java imports and
  interface/enum/record symbols; the bootstrap detects the Maven/Gradle layout. (Gson:
  0 → 519 import edges; 121 → 2 unreachable.)

### Fixed
- **`codd extract` crashed on proto enums.** An api-contract template accessed
  `schema.values`, which Jinja resolved to the dict's `.values` method (then `| join`
  raised); any project with proto enum schemas crashed out of the box. It now uses
  `.get("values")` with a uniform renderer for proto/GraphQL value shapes.

## [3.7.2] - 2026-06-25 — brownfield reachability + JS (Express) dogfood fixes

### Added
- **Doc-less brownfield reachability.** `transitive_closure` now seeds reachability from
  code-entry roots (impl source nodes with outgoing but no incoming import edges) in
  addition to design-doc roots, so a project with no design docs is measured from its code
  structure instead of being reported entirely unreachable. Genuine orphans are still
  flagged and doc-rooted projects are unchanged. (Flask 46→12 unreachable; Express 6/6
  reachable from the auto-detected entry.)

### Fixed (surfaced by the Express brownfield dogfood)
- **CommonJS `require()` was invisible to JS import extraction.** The tree-sitter extractor
  visited only `import`/`export` statements (not `require()` / dynamic `import()` call
  expressions) and the regex fallback matched only ESM specifiers, so a CommonJS project's
  dependency graph came out empty. Both paths now capture `require()` / `import()`.
- **AI extraction degenerated with tools disabled.** The extract prompt instructed the model
  to shell out, but the default `ai_command` passes `--tools ""`; the model went agentic and
  produced stub output. The prompt now detects tool availability and, when disabled, directs
  extraction from the embedded project context.
- **Bootstrap hardcoded `tests/`/`docs/`**, and the DAG's test-file rule required a `.test.`
  infix — so a project using `test/*.js` (e.g. Mocha) had its tests silently excluded. The
  bootstrap now detects the actual test/doc directories and any source under a test
  directory counts (Python keeps its stricter `test_*.py` rule). Express: 0 → 91 test nodes.
- **Route paths were mis-extracted from JSDoc comments.** Comments are stripped before route
  detection.

## [3.7.1] - 2026-06-25 — Flask brownfield dogfood: real-OSS-found fixes

Pointing CoDD at a real external OSS (Flask) — human-authored code outside the design
loop — surfaced bugs the self-built fixture corpus could not.

### Fixed
- **Python dependency graph was empty.** The DAG import-edge extractor matched only quoted
  (JS/TS) specifiers, so Python impl files yielded zero import edges and
  reachability/transitive-closure was silently neutered for every Python project. Import
  extraction is now AST-based (`codd/parsing/python_ast.py`), resolving both absolute and
  relative imports (`from .x import y`); dispatch is by file type, not a language branch.
  Flask went from 0 to 94 impl→impl import edges.
- **`ci_health` missed `.yaml` workflows.** The default glob was `.github/workflows/*.yml`,
  RED-failing every project whose CI uses the equally-valid `.yaml` extension. It now
  matches both `.yml` and `.yaml`.
- **`codd extract --ai` silently dropped documents.** The parser split only on exact
  `--- FILE: ---` markers and did not strip ```` ```markdown ```` fences, so a fenced
  multi-document model reply lost most of its output and corrupted the survivor's
  frontmatter. It now strips fences, falls back to frontmatter / `# L<n>:` header
  splitting, writes the raw output before parsing, and warns when recovered docs are fewer
  than the expected MECE layers.
- **Bootstrap template emitted a dead `context_acquisition` key** that the config schema
  rejected, warning on every fresh project. Removed (no code reads it).

## [3.7.0] - 2026-06-25 — N-gate systematic hardening + Axis-P owner-free coverage

### N-liveness gate (Stage 1) — systematic false-green / path-escape closure
- Unified FS root-jail in new `codd/path_safety.py` (resolve → follow symlinks →
  `relative_to(project_root)`; `PathEscapeError` / `require_project_path` fail-closed).
  Every user-path-controllable evidence reader (codd.yaml config, design-doc frontmatter,
  DAG node.path, CLI path args, fixed-filename, import-specifier) across the dag checks,
  builder, extractors, propagator, and cli routes through it. Absolute-in-root glob
  patterns are rebased (no false-RED); a declared-evidence path that escapes the project
  now fails closed instead of being silently skipped.
- Visibility centralized in new `codd/dag/result_status.py` (status-aware
  `result_has_findings` / `pass_is_warn`), bound by cli + coverage_metrics + deployer.
  All three text verify summaries and the `--format json` output consistently render
  SKIP / WARN / vacuous — a red/warn result is never shown as a clean PASS, and a merge-gate
  metric never counts a SKIP as covered. Every registered check exposes `checked_count`
  or skips; skip results carry `severity="info"`.
- Confirmed by two consecutive multi-engine systematic-clean review rounds.

### Axis-P coverage (Stage 2) — owner-free
- Owner-free gap flow: a model/structural coverage gap becomes an amber
  `AskItem(blocking=false)` persisted in `coverage_decisions` (CI/merge never wait); a
  CONFIRMED decision is promoted to an explicit contract (`gap_kind→contract_key` routing,
  overridable via `codd.yaml` `axis_p.gap_routing`) that the existing deterministic checks
  then enforce as red. RED comes only from explicit/confirmed/closed-world contracts;
  model confidence stays amber recall.
- `codd check` now reports positive coverage materiality (contracts / covered / pending /
  gap). New coverage metrics: E-PCUMR (explicit-contract coverage for real projects) and
  corpus PCUMR (frozen + construction-derived gold).

## [3.6.0] - 2026-06-24 — Self-hosted coherence gate

Shipped via the **Self-hosted Coherence Gate** milestone — codd-dev's own changes
run through CoDD's design → test → implement → verify loop (CoDD verified by CoDD).
See `quality/self_hosting_ledger.md`.

### Added

- **`malformed_contract` amber** in `resource_flow_coherence` — a declared contract
  entry missing its required field (a `consumes`/`produces` without `resource`, a
  `capability_contracts` entry without `capability`) is now surfaced instead of
  being silently dropped.

### Changed

- **Every `resource_flow_coherence` finding carries an actionable `remediation`** —
  `dangling_required_consumer` reds and the `dead_resource` / `malformed_contract` /
  `unscoped_resource_consumer` ambers are now self-repairable, not just diagnosed.
- **`codd dag verify` prints a SKIP count** ("N check(s) SKIP — verified nothing")
  so a run riddled with silent skips is visibly not a full verification.
- **`resource_flow_coherence` PASS reports how many resource uses it checked**, so a
  pass is transparent about its coverage.

## [3.5.0] - 2026-06-24 — Skip visibility

### Fixed

- **`codd dag verify` now shows skipped checks as `SKIP`, never `PASS`** (both the
  default and the detailed summaries). A check that skips — because it is dormant,
  unconfigured, or missing its input — verified nothing, yet was previously
  rendered as `PASS`, making a run riddled with silent skips indistinguishable
  from a genuinely clean green run. The change is visibility-only: gate logic and
  exit codes are unchanged. Surfaced by the false-green hardening loop
  (`quality/false_green_vectors.yaml`, vector `silent_skip_shown_as_pass`).

## [3.4.0] - 2026-06-23 — Dead-resource detection

### Added

- **`dead_resource` amber warning** in `resource_flow_coherence` — the mirror of
  `dangling_required_consumer`. A contract resource that has a producer obligation
  but **no consumer** (produced-but-never-consumed) is now surfaced as an amber
  advisory instead of passing silently. Amber only: never red, never blocks
  deploy; externally-provided resources are exempt; dormant when no contracts are
  declared. First vector closed by the false-green hardening loop
  (`quality/false_green_vectors.yaml`), implemented Claude+Codex in parallel.

## [3.3.0] - 2026-06-23 — Resource-flow coherence

### Added

- **`resource_flow_coherence` DAG check** — the data-field sibling of the
  enablement axis. Where the enablement axis asks "is a *capability* that gets
  exercised also granted?", this check asks the same of *data resources*: a
  required capability that **consumes** a contract resource is now flagged when
  no obligation **produces** that resource ("dangling required consumer"). This
  closes a false-green class where a data slot is read by a required capability
  but written by nothing — e.g. a per-person notification that resolves a
  recipient id no flow ever populates would previously verify green yet never
  reach anyone.
  - Contract-declaration driven via new design-doc frontmatter attributes
    `capability_contracts` (`consumes` / `produces`) and `resource_contracts`
    (`consumers` / `producers` / `externally_provided_by` / `aliases`). The core
    reasons only over canonical resource ids and produce/consume relations —
    **no implementation literal scanning**, so it stays language / framework /
    project agnostic.
  - **RED only when all hold:** the consumer is required (`required: true` or
    `on_missing: fail`), its capability is a required capability of a
    `critical`/`high` user journey, and no producer / external provider / seed
    exists for that resource. Weaker reads (optional, `on_missing: skip|degrade`,
    capability not on a critical journey) are advisory, not gated.
  - **Dormant by default:** projects that declare no resource/capability
    contracts get `skip`, so existing projects keep passing unchanged.
  - Kept deliberately separate from the enablement axis (`enables`/`exercises`
    is the capability supply relation; `produces`/`consumes` is the resource
    supply relation) to keep severity and diagnostics clean.

## [3.0.0] - 2026-06-21 — Contract Kernel

**CoDD's core no longer hard-codes any language or framework.** The kernel
drives the full **design → test → implement → verify** loop from declarative
profiles + adapters, so support for a new language or framework is added
*outside* the core, never by editing it.

### Headline

- **Language-free core.** Go, Python, and TypeScript are driven entirely by
  declarative `LanguageProfile`s and adapters. The core never branches on a
  language name; a new language is added with a profile + adapter and no core
  change (locked by a static gate and proven by a synthetic-language test the
  core has never seen).
- **Framework-pluggable stack.** Frameworks (e.g. Next.js) and addons
  (Playwright, Prisma) compose with the language into one *resolved stack
  contract* that `greenfield` and `verify` consume live. A new framework plugs
  in with a profile + checker and no core change. Declare a stack in `codd.yaml`
  under a `stack:` block; it is resolved, locked (`codd.stack.lock`), and
  enforced — lock drift fails RED rather than silently refreshing.
- **Anti-false-green verification, owned by the core.** A build/test run cannot
  report green without proof. Empty tests, no-op commands (`"build": "true"`),
  missing reports, disabled checkers, stack-lock drift, weakened obligations,
  and seeded source mutations all fail RED. Profiles may configure parameters
  but can never weaken this invariant.

### Verified

- A real **Next.js** app (App Router + TypeScript + Playwright + vitest) driven
  end-to-end through the live verify pipeline on the actual toolchain, with a
  green baseline plus a battery of negative controls that each fail RED for the
  correct reason.
- Green on the Python **3.10 / 3.11 / 3.12** CI matrix.

### Breaking

- The implement/verify oracle path is now contract-driven. The legacy
  `source_root` shortcut is removed from the v3 oracle path; output routing and
  layout come from the resolved language/stack profile. An unsupported
  implement-oracle request is now an explicit RED instead of a silent pass.

## [2.77.0–2.102.0] - 2026-06-21 — Contract Kernel build-up

The incremental work that produced v3.0.0. Each release moved one more piece of
language/framework knowledge out of the core and behind a declarative contract,
while keeping the anti-false-green guarantees intact.

### Added — language-free oracle path

- **Oracle adapter infrastructure**: a generic executor + protocol so each
  language's implement-time oracle (build/compile/symbol checks) is a pluggable
  adapter. Go, Python, and TypeScript oracles now run on the contract path; an
  unsupported language is an explicit RED, never a silent pass.
- **Generic, kind-agnostic oracle resolution**, validated with a synthetic
  language unknown to the core.

### Added — framework-pluggable stack

- **`FrameworkProfile` / `AddonProfile` schema** plus a framework/addon loader
  and registry, with curated profiles for **Next.js, Prisma, and Playwright**.
- **Resolved stack contract** composed from language + framework + addons,
  written to `codd.stack.lock`. `codd.yaml` gains a `stack:` block that resolves
  to a contract consumed by `greenfield` and `verify`.
- **Stack enforcement gates**: lock-drift detection (RED, no silent refresh),
  materialization + conflict checks, command authenticity (exit 0 is not
  enough), and a framework-obligation checker that drives red/green.

### Changed — dispatch is data-driven, not literal

- Verify-runner node detection, project layout/scaffold selection, and the CEG
  parsing/extraction dispatch are now profile-driven and registry-data-driven
  rather than keyed on hard-coded language strings.
- Static gates enforce that the generation and oracle-dispatch cores stay free
  of language- and framework-literal branching, so the invariant can't silently
  regress.

### Added — anti-false-green, core-owned

- Conformance with the anti-false-green contract is now owned and enforced by
  the core (closing latent stack false-green paths), with a CI-enforced profile
  conformance contract proving extensibility.

## [2.68.0–2.76.0] - 2026-06-21 — Contract Resolution Seam + verify switch

Foundation for the Contract Kernel.

- **Contract Resolution Seam + minimal adapter registry** — the seam every later
  language/framework profile plugs into.
- **Unweakenable verify observation policy** — a verify run's observation rules
  cannot be weakened by configuration; the verify executor classifies results
  with anti-false-green semantics and closes the "unit-tests-only PASS" hole by
  observing verify scope.
- **Contract-driven CI setup** — removed the hard-coded CI-setup steps in favor
  of a contract; runner-report adapters were extracted to a lazily-registered
  leaf module.
- **VerifyRunner contract-first switch** completes the verify spine, with
  cwd/env layout placeholders substituted at run time.

## [2.41.0–2.67.0] - 2026-06-17 … 2026-06-21 — Multi-language oracle + authenticity gates

A long run of anti-false-green hardening across languages, plus the first live
multi-language gates that became the v3.0 oracle path.

### Added

- **Python composite implement-oracle** — an undefined first-party import or
  symbol now fails RED at implement time. Companion fixes credit Python
  helper-delegated assertions across barrels, locals, and decorators, and excuse
  legitimate patterns (PEP 562 module `__getattr__`, conditional /
  `TYPE_CHECKING` imports, uninstalled third-party imports during collection).
- **Go composite oracle** (`go build` + `go vet`), a **Go `test_semantics`
  adapter** (the first live profile-driven gate), a **`go-test-json`
  runner-report adapter** for per-behavior execution evidence, and Go-aware
  output routing (repo-root layout, `-buildvcs=false`).
- **`ci_scaffold` greenfield stage** — authors authentic CI so a generated
  system is CI-ready out of the box.
- **Authenticity gates**: reject constant-only and library-only "direct"
  assertions; require positive execution evidence for `pytest`-over-HTTP tests
  (exit 0 alone is insufficient); a strict-observability gate where a recognized
  test file with no parseable test is RED, not a silent degrade.
- **Language-aware web E2E harness selection** and a toolchain-coherence
  preflight for Node test harnesses.
- **Declarative `LanguageProfile` foundation** (additive, ahead of live wiring)
  and a profile-driven output router so each language gets the right layout.

### Fixed

- Numerous cross-language **false-RED** corrections so valid test structures
  (multi-line signatures, `raise AssertionError(...)`, `with self.assertRaises`,
  unittest assert helpers, package/barrel helper chains) are credited correctly,
  with parity locked across Python and TypeScript.
- `declared-output-completeness` no longer misreads a symbol or a node id as a
  file path, and now recognizes multi-segment directory outputs.

## [2.23.0–2.40.0] - 2026-06-14 … 2026-06-17 — Greenfield robustness + ACG Contract Registry

Hardening of the unattended `greenfield` pipeline and the verifiable-behavior
(VB) coverage model, plus a proactive coherence-contract layer.

### Added

- **First-class TypeScript verify + repair** — scaffold/install, `vitest` + `tsc`
  attribution, `.e2e.ts` suffix recognition, and e2e routing. An implement-time
  native-oracle gate runs `tsc` at stage level.
- **Single canonical verifiable-behavior (VB) registry doc** with a collision
  validator, closing a "0/0 coverage" false-green; the VB coverage gate now runs
  once per implement stage rather than per task.
- **Static coherence gates**: a no-runtime-import e2e contract, a test-helper
  symbol-import gate, and a confusable/homoglyph detector that catches non-ASCII
  lookalikes the parser would otherwise accept.
- **ACG Contract Registry + certification matrix** — proactive coverage of
  coherence contracts (owner-uniqueness, campaign-observable, deterministic
  design-doc reference resolution) instead of fixing one false-green at a time.
- **VB-marker traceability anti-false-green gate** (marker authenticity +
  test-scoped rerun), a coverage-execution coherence gate (verify campaign
  reconciled against per-VB execution), and a dependency manifest↔lock coherence
  / lock-freshness barrier so a frozen install consumes a fresh lock.

### Changed / Fixed

- The orphan-artifact gate now defaults to **enforce**, with scaffold/harness
  ownership made explicit.
- The implement-time oracle rerun is **scoped to the broken edge** (broad rerun
  demoted to a fallback), with an oscillation-aware repair loop using
  contract-aware feedback and targeted edits.
- Verification-test commands run with `cwd=project_root`.
- A wall-clock timeout on AI subprocess calls prevents silent hangs; transient
  transport / output-token-ceiling errors auto-recover; file-writing agents may
  deliver output via the stdout contract, not only on disk.
- Python 3.10 robustness: `tomllib` → `tomli` fallback in the syntax/oracle path.

## [2.21.0–2.22.0 line] - 2026-05 … 2026-06 — Greenfield autopilot, model independence, operational E2E

Shipped across the 2.20 → 2.23 window (the "v2.22.0 mechanisms").

### Added — greenfield autopilot

- **`codd greenfield` unattended autopilot** — write a requirements document,
  walk away, and CoDD generates, repairs, and verifies a working system for
  every supported CLI setup. The patchwork generation seams were consolidated
  into one frontmatter / AI / discovery / confidence layer with a unified CLI
  surface.

### Added — model-independent harness

- A harness-owned layout profile, scaffold, and AST import-coherence gate so the
  generated source and tests share one package context, plus contract-aware
  task-done verification — the harness produces the same coherent result
  regardless of which model (strong or weak) drives it.

### Added — `enables` relationship + freshness ledger

- **`enables` operation relationship** with enablement-coverage axes
  (`enablement_chain`, `access_path_variation:*`), a third `grant_verbs` class in
  `capability_completeness`, and an `[enables_nudge]` doctor advisory.
- **Doc-to-doc `dependency_freshness` check + reconciliation ledger** — durable
  state for whether an upstream document change has been reconciled downstream;
  `codd propagate --commit` writes ledger acknowledgements. Default severity is
  amber (opt in to red); fully opt-out with `enabled: false`.
- All of the above are opt-in or advisory: a project without an `enables`
  declaration derives a byte-identical scenario set, and exit codes are unchanged
  unless `dependency_freshness.severity: red` is configured.

### Added — operational / runtime E2E

- **Runtime CRUD-flow verification** (`codd verify --runtime` `crud-flow`
  category) and **runtime action-outcome coverage** (`action-outcome` category),
  so command targets are not "documented by exit code alone."
- **Operational E2E audit** with a `cross_route_state_restore` axis: SPA route
  re-entry state (draft forms, carts, list filters/scroll position, multi-step
  wizards) now requires a client-side round-trip proof; a reload-only readback is
  explicitly insufficient.

### Fixed

- Reported CLI / robustness bugs: `codd init` works in non-TTY environments
  ([#29](https://github.com/yohey-w/codd-dev/issues/29)), `codd propagate-from`
  no longer aborts on `date` frontmatter
  ([#28](https://github.com/yohey-w/codd-dev/issues/28)), and `codd generate` /
  `plan --init` tolerate noisy AI output for `wave_config`
  ([#27](https://github.com/yohey-w/codd-dev/issues/27)).
- DAG-check robustness: `implementation_coverage` now matches
  `common`-reclassified code and resolves literal / bracketed-path hints against
  the file system; `ci_health` no longer collects deploy-hook labels as commands.

## [2.20.0] - 2026-05-18 — Codex App Server JSON-RPC integration

### Added

- **Codex App Server provider** — an opt-in JSON-RPC `AiCommand` backend
  (stdio / unix / ws transport) used by `codd implement-step`,
  `codd verify --auto-repair`, and `codd fix [PHENOMENON]`. Configure it under a
  `codex_app_server` block in `codd.yaml` (`enabled: false` by default), with
  fields for transport, model, effort, timeout, thread strategy, and fallback.

### Compatibility

- The existing subprocess `AiCommand` path is unchanged; a `codd.yaml` without a
  `codex_app_server` block behaves exactly as before.

## [2.19.0] - 2026-05-16 — Full OSS release: Pro Gate removed

### Changed

- **`codd verify` runs the verifier directly** — `codd-pro` is no longer required
  for verification.

### Removed (breaking)

- Removed the `codd review`, `codd audit`, and `codd risk` commands. These never
  had a real implementation behind the Pro gate (0 affected users). The plugin
  system itself is retained for future third-party plugins.

## [2.16.0–2.18.0] - 2026-05-11 … 2026-05-12 — `codd fix [PHENOMENON]` + greenfield triage

### Added

- **`codd fix "<phenomenon>"`** — CoDD's second North Star entry point: describe
  what you want fixed in natural language and CoDD selects the relevant design
  docs, applies an interactive HITL gate for ambiguous / risky changes, rewrites
  the design doc (frontmatter byte-for-byte protected), and runs the DAG +
  pytest gate. New flags: `--dry-run`, `--non-interactive`, `--on-ambiguity`,
  `--allow-delete`. Legacy `codd fix` (test-failure mode) is unchanged.

### Fixed

- **Greenfield triage** (Issues [#20](https://github.com/yohey-w/codd-dev/issues/20)
  / [#21](https://github.com/yohey-w/codd-dev/issues/21) /
  [#22](https://github.com/yohey-w/codd-dev/issues/22)): `codd implement run`
  gains a per-invocation `--language` override (no project re-init needed);
  `detailed_design` is recognized as a valid node prefix; and the code-fence
  stripper no longer leaks trailing markdown into generated source.
- **Emergency packaging patch** (v2.17.1, Issues
  [#23](https://github.com/yohey-w/codd-dev/issues/23) /
  [#24](https://github.com/yohey-w/codd-dev/issues/24)): `codd fix` prompt
  templates (`*.txt`) now ship inside the wheel, and template delimiters use
  XML-style tags so they no longer collide with markdown frontmatter.

## [2.12.0–2.17.0] - 2026-05-10 … 2026-05-11 — `kind: common`, completeness gates, opt-out protection

### Added

- **`kind: common` DAG node kind** for shared infrastructure (DB clients,
  middleware, framework config, shared utilities) — declared via
  `codd.node_type: common` frontmatter or `common_node_patterns:` globs in
  `codd.yaml`. Common nodes skip the transitive-closure amber but still
  participate in impact analysis. (v2.17.0 extends the fix to
  `node_completeness`.)
- **Test-completeness gates**: a `ci_health` static check (CI workflow exists,
  declares the required triggers, and references every post-deploy verification
  test) and actor → user-journey coverage findings.
- **Opt-out protection** — any check with a config-level opt-out must carry a
  justification and a future-dated expiry in `codd.yaml` `opt_outs:`; silent SKIP
  is abolished, and a check's severity is preserved rather than skipped away.

## [2.7.0–2.11.0] - 2026-05-08 … 2026-05-10 — Sprint-less implement, lexicon expansion

### Changed (breaking)

- **Sprint-less `codd implement`** — `codd implement` now takes a design node and
  output paths directly (`--design`, `--output`, `--depends-on`); `--wave` and
  `implementation_plan.md` auto-detection are removed. See the migration cookbook
  in `docs/migrations/v2.11.0-sprintless.md`.
- **Default scope is now `system_implementation`** (was `full`): when a project
  doesn't declare `scope:`, CoDD suppresses business-management dimensions
  (goal / KPI, UAT detail, risk register, glossary) from `codd elicit`, matching
  its role as a coherence verifier rather than a project-management tool.

### Added

- **+7 cross-industry lexicons** (38 total, ~60 new axes): i18n / Unicode-CLDR,
  Twelve-Factor App, OWASP MASVS, Domain-Driven Design, DORA / SRE metrics, ML
  Model Cards, and API rate-limiting / caching.
- **LLM-enhanced `codd init --suggest-lexicons`** (`--llm-enhanced`) that
  recommends lexicons from detected data types and function traits (e.g.
  "personal information → personal-data governance lexicon"), with a confidence
  and reason per recommendation and graceful fallback to the regex-based path.

## [2.1.0–2.6.1] - 2026-05-07 … 2026-05-08 — Lexicon coverage gate, deployment auto-discovery

### Added

- **`extends:` lexicon namespace + multi-lexicon auto-load** so a project can
  compose several lexicons (legacy `suggested_lexicons:` still loads, with a
  deprecation warning).
- **Deployment chain auto-discovery** and **runtime-state auto-binding** for the
  deploy / verify pipeline, so chains like `Dockerfile → build artifact` close
  without a `codd.yaml` change.
- **Lexicon coverage CI gate** (`codd coverage check` with configurable
  thresholds) plus elicit scope / phase filtering and elicit-apply bug fixes.
- The AI-call timeout default was raised to **30 minutes** (a value that causes
  frequent timeouts is not a valid default; override via argument, env var, or
  `codd.yaml`).

## [2.0.0] - 2026-05-07 — Lexicon-Driven Completeness

**A positioning shift: write only functional requirements and constraints — code
is generated, repaired, and verified automatically.** v1.x delivered the
extract → diagnose → repair pipeline; v2.0 adds the *constraint* side as a
first-class plug-in surface, so industry standards become mechanically reusable
coverage axes.

### Added

- **31 lexicon plug-ins across 7 domains** (BABOK / WCAG / OpenAPI / OWASP /
  ISO 27001 / GDPR / Kubernetes / OpenTelemetry / …), ~280 coverage axes total.
- **`codd elicit`** — a coverage / spec-discovery engine with lexicon-aware
  coverage mode (gap-only findings).
- **`codd diff` / `codd brownfield`** — drift detection and brownfield
  orchestration (extract → diff → elicit).
- **`codd lexicon list/install/diff` + `codd coverage report`** — plug-in
  management plus JSON / Markdown / self-contained HTML coverage matrices.
- **`codd init --suggest-lexicons`** — manifest-file scan suggests lexicons.
- **RepairLoop strategy v2** — a generic fallback chain replaces stack-specific
  hardcoding.
- **Three-layer Generality Gate** — core (zero project-specific literals) /
  templates / plug-ins.

### Compatibility

- All v1.x CLI commands and config keys remain functional; v2.0.0 adds
  subcommands and removes nothing.

## [1.22.0–1.42.0] - 2026-05-05 … 2026-05-07 — DAG completeness, deploy verification, lexicon foundation

The late-v1 milestone run that built CoDD's coherence-gate and lexicon
machinery.

### Added

- **DAG Completeness Gate** (`codd dag build` / `codd dag verify`) — design docs,
  implementation files, plan tasks, and expected values are one DAG, checked for
  completeness (node / edge / dependency / task / transitive-closure).
- **Change-driven auto-propagation pipeline** — `codd watch`,
  `codd propagate-from --files`, `codd test --related`, and ready-to-use
  Claude / Codex / git hook recipes, so an edit triggers impact → propagate →
  verify → fix and runs only the related tests.
- **Deploy Verification Gate** (`codd deploy`) and a **User Journey Coherence
  layer (C7)** that verifies the design → deploy → runtime → verification chain.
- **CDP-browser E2E + LLM test-generation pipeline** and an **auto-repair +
  polyglot** implement path (9+ languages).
- **`codd elicit`** introduced (v1.35.0) as the coverage / spec-discovery engine,
  ahead of its v2.0 expansion.
- **Brownfield pipeline** (`codd extract` → `codd diff` → `codd elicit`
  orchestration) and the first **30-lexicon** library with `codd init`
  auto-suggest and a lexicon coverage CLI.

## [1.11.0–1.21.0] - 2026-05-04 … 2026-05-05 — Coherence engine + AI-driven design

### Added

- **Coherence engine** — a unified drift-event hub with severity routing
  (`red` → auto-fix, `amber` → pending HITL, `green` → log) and the
  `codd fixup-drift` CLI with worktree-isolated apply.
- **Meta-design context layer** (`project_lexicon.yaml`), filesystem-routing-aware
  drift detection (`codd drift`), and `DESIGN.md` design-token integration
  (W3C Design Tokens).
- **AI-driven design-artifact derivation** (`codd plan --derive`,
  `codd require --completeness-audit`), screen-transition / screen-flow edge
  support, autonomous preflight (`codd preflight` / `codd gungi`), and
  end-to-end E2E generation (`codd e2e-generate`).
- `codd deploy`, `codd propagate --reverse`, and `codd require --propagate`
  complete the bidirectional propagation paths.

## [1.0.0–1.10.0] - 2026-03-31 … 2026-04-19 — First stable line

### Added / Changed

- First **1.0** release line of the core graph engine, impact analysis, and the
  generate / verify / implement commands.
- **Multi-language `codd implement`** — honors `project.language` (12 languages
  via `LANGUAGE_EXT_MAP`) instead of hardcoding TypeScript
  ([#12](https://github.com/yohey-w/codd-dev/issues/12)), plus multi-AI-engine
  support (file-writing agents such as Codex via git-diff capture) and
  intra-phase parallel execution with worktree isolation.
- **Diagnostic reasoning in `codd fix`** — the AI must produce a root-cause
  diagnosis before patching, with prior-attempt history injected on retries
  (SWE-bench Verified: 73/73 resolved, up from 28/30).
- **OSS/Pro split** (v1.6.0, "Bridge Release") — `review` / `verify` / `audit` /
  `risk` moved to a `codd-pro` package; later reversed in v2.19.0 when the Pro
  gate was removed.
- Batch guard for `codd implement` (`--max-tasks`).

### Fixed

- `codd hooks install` no longer fails after `pip install` — `hooks/` is packaged
  inside the wheel ([#1](https://github.com/yohey-w/codd-dev/issues/1)).
- `codd measure` dict-attribute crash, `codd validate` false "undefined" node
  reports, and `codd extract → plan --init` on brownfield projects with no
  `codd.yaml` ([#2](https://github.com/yohey-w/codd-dev/issues/2),
  [#3](https://github.com/yohey-w/codd-dev/issues/3),
  [#4](https://github.com/yohey-w/codd-dev/issues/4)).

## [0.2.0a1–0.2.0a5] - 2026-03-29 — Public alpha

### Added

- First public alpha of CoDD (renamed from CPDD). The core graph engine and
  impact analysis are stable; generation and verification are experimental.
- **V-Model verification phases** (unit → detailed design, integration → system
  design, E2E → requirements) with a test strategy derived from architecture.
- **`codd extract`** — brownfield bootstrap that reverse-engineers CoDD design
  documents from existing code using pure static analysis (no AI), supporting
  Python, TypeScript, JavaScript, Go, and Java.
- Core CLI: `codd init`, `codd scan`, `codd impact`, `codd validate`,
  `codd generate`, `codd verify`, `codd implement`, `codd plan --init`,
  `codd hooks install`.
- Frontmatter as the single source of truth; the graph store migrated from
  SQLite to portable JSONL.

## [0.1.0] - 2026-02-15 — Initial internal release

### Added

- The Conditioned Evidence Graph (CEG) with a JSONL-backed dependency graph.
- `codd init`, `codd scan`, `codd impact`.
- Frontmatter-first architecture and convention-aware impact propagation with
  Green / Amber / Gray confidence bands.
