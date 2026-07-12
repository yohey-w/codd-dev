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

## [3.35.0] - 2026-07-12 — Feat: regeneration parity — the mechanical contract flows to the repair path so v3.33.0's signature convergence survives repair

v3.33.0 established "convergence granularity ≤ circulated contract granularity" and made producer
SIGNATURES circulate to consumers at first-pass implement. But the **repair regeneration path**
(`codd/repair/llm_repair_engine.py`) regenerated code WITHOUT that mechanical contract, so a single
repair could undo the convergence and re-open Root A (TS2835 module-specifier) and Root C (TS2307
runtime-dep) — a repair that adds one import carried neither the specifier convention nor the manifest
obligation. This release adds the **monotonicity invariant**: a repair regeneration prompt's
mechanical-contract context ⊇ the first-pass implement prompt's (at signature granularity).

- `codd/implementer.py`: extracts the three profile-DATA mechanical-contract blocks (namespace /
  module-specifier / runtime-dependency), formerly assembled inline in `_build_implementation_prompt`,
  into a shared pure/deterministic builder `build_mechanical_contract_context(project_root, config, *,
  profile, target_files)`. Behaviour-identical (byte-identical output) for the first-pass caller; the
  extraction exists only so the SAME contract can also feed the repair path. Profile-DATA dispatch is
  preserved — no `language ==` branch, no domain/framework literal.
- `codd/repair/llm_repair_engine.py`: `propose_fix` now injects, per proposal, a regeneration-parity
  block into the propose / strategy prompts (new `{mechanical_contract}` template value, empty when
  nothing resolves so the prompt is otherwise unchanged): (a) the three mechanical blocks via the shared
  builder, and (b) for each SOURCE (non-test) file under repair, the **disk-measured distance-1 producer
  imports** (`resolve_local_import_targets`) rendered at **SIGNATURE granularity** (`render_public_surface`
  → names → path). Signature is floor AND ceiling — a producer **body is never rendered** (post-verify, a
  body is a false-green vector). A **test-unit repair stays SUT-blind**: producers are resolved only from
  non-test consumers, so a pure test repair carries the mechanical blocks alone (no SUT signature surface,
  preserving the F7.1 impl-blind firewall). No global cache — distance-1 differs per repair unit.
- Test re-derivation (`codd/greenfield/test_rederivation.py`) receives the mechanical blocks for free via
  the shared builder on the `implement_tasks` path, and no SUT signature surface is added to it (the
  repair-signature injection is confined to `propose_fix`).
- Steer-only: no new gate / node / artifact, zero diff to the write-fence / ownership / F7.1 semantics.
  Generality-safe: the distance-1 producer + signature ladder dispatch on file extension (TS today); a
  renderer-less language degrades to names, then paths — the same graceful degradation as implement.

## [3.34.2] - 2026-07-12 — Fix: robust AI-payload write + repair-proposal KeyError symmetry (run-killer residuals of the v3.34.1 class)

Two small robustness fixes closing run-killer residuals of the same class as v3.34.1 — a raw, unhandled
exception in a core stage that escapes and crashes an otherwise-recoverable run.

- `codd/implementer.py` `_write_generated_files`: guards the AI-payload **write** site against a path-kind
  collision, mirroring the v3.34.1 fix in `_create_output_paths` (which guarded only the declare-time `mkdir`).
  If a payload's destination already exists on disk as a **directory**, `write_text` raised a raw
  `IsADirectoryError`; if an ancestor path component is itself a **file**, `mkdir` raised a raw
  `NotADirectoryError`/`FileExistsError`. Both now surface as a clean `StageError` (`path-kind collision`,
  naming the task and path) routed into the autopilot's existing clean-red/regenerate path. Path-SHAPE/kind
  only — no language/framework knowledge; reuses the same lazy-imported `StageError` and message style.
- `codd/repair/llm_repair_engine.py` `_repair_proposal`: adds `KeyError` to the proposal-schema handler,
  restoring symmetry with the sibling `analyze` handler (which already caught `KeyError`). A malformed proposal
  (e.g. a patch entry missing the required `file_path` key, accessed in `_file_patch`) now becomes the module's
  existing retriable `RepairFailed` (schema mismatch) instead of a raw `KeyError` that escaped `propose_fix` and
  crashed the repair loop.

Generality-first (path-shape / exception-type logic only; no `language ==`, no domain literal in core),
red-before-green for both fixes, full suite 7452 passed / 1 xfailed / 0 skipped, `language`-free-core ratchet
green. Release content for the separately-gated v3.34.2.

## [3.34.1] - 2026-07-12 — Fix: robust file-path outputs (IsADirectoryError from v3.34.0's VB-coverage task)

Robustness fix completing v3.34.0. The v3.34.0 VB-coverage-closure task was the **first** task to declare
concrete FILE paths as `output_paths` (the residual VBs' owner test files). But `_create_output_paths`
(`codd/implementer.py`) blindly `mkdir`'d every declared output path as a **directory** — turning those file
paths into empty directories — so the subsequent `write_text` raised a raw, unhandled `IsADirectoryError` that
crashed the implement stage (surfaced by an S3 StockRoom-mini re-burn). The sibling `_clean_output_paths` already
discriminated file vs directory; `_create_output_paths` did not.

- `codd/implementer.py` `_create_output_paths`: now discriminates file-shaped vs directory-shaped declared
  outputs using the codebase's own `_declared_output_is_file_path` predicate (no new heuristic). A file-shaped
  output (e.g. `tests/e2e/helpers/workspace.ts`) creates only its **parent** directory (the file itself is
  written later); a directory-shaped output (`src`, `tests`) keeps the prior `mkdir`.
- Honest-red hardening: a file-shaped output that already exists on disk as a directory (or a directory-shaped
  one that exists as a file) now raises a clean `StageError` (`path-kind collision`) routed into the autopilot's
  existing clean-red/regenerate path, instead of letting a raw `IsADirectoryError`/`FileExistsError` escape.

Generality-first (path-shape logic only; no `language ==`, no domain literal in core), red-before-green, full
suite 7448 passed / 1 xfailed / 0 skipped. Surfaced by the StockRoom-mini calibration burn (internal R&D).

## [3.34.0] - 2026-07-12 — S3-mini R&D yield #2: plan-time VB-coverage closure (every declared behavior gets an owning task)

Second yield of the S3 real-service-scale greenfield R&D. With v3.33.0's implement-time typecheck classes
cleared, the StockRoom-mini re-burn advanced past the typecheck gate and honest-stopped one gate later — the
verifiable-behavior (VB) coverage gate — with 10 of 41 declared VBs carrying no `codd: covers vb=` marker after
all implement tasks completed. Root cause was **plan-stage**, not implement: those 10 VBs (static-source/manifest
invariants, suite-level meta-completeness assertions, and universally-quantified cross-route invariants) are
structurally cross-cutting and were never decomposed into an owning test-authoring task — so no task could ever
emit their marker, and the coverage gate's own repair loop was inert (it re-ran only the no-authoring registry
task). Generality-first, red-before-green, and the coverage gate itself is unchanged (still fails closed).

- **Plan-time VB→task coverage closure** (`codd/planner.py`): new `synthesize_vb_coverage_closure_task`
  extends the existing "the VB registry document is planned" guarantee by one level — after task derivation it
  diffs the declared VBs against the VBs claimable by any derived task (a task whose `expected_outputs` contain
  the VB's owning test file) and, for the residual set, synthesizes one cross-cutting test-authoring task that
  owns exactly those VBs (design-node = the canonical registry doc; prompt = the residual VB rows + owner-file
  appendix + the standard determinism/isolation harness contract). Reuses the existing VB parse/audit path; no
  new gate or first-class concept. When every declared VB is already claimable, nothing is synthesized
  (no-regression for projects whose behaviors all map to a source module).
- **Derive-stage wiring** (`codd/greenfield/pipeline.py`): `_enforce_vb_coverage_closure` runs in the implement
  stage before the per-task loop and the coverage-gate wiring, so the synthesized task is both implemented and
  visible to the gate's rerun scope.
- **Repair-loop de-inerting** (`codd/vb_rerun_scope.py`): when the gate's stage-1 rerun scope resolves only to
  a no-authored-artifact task (the doc-only registry task, whose `test_strategy.md` filename misleadingly matches
  the test-task heuristic), it is dropped and the scope falls through to the tasks that actually author test
  files — the precise reactive path for the exact uncovered set, write-fenced to the test surface.

Surfaced by the same StockRoom-mini calibration burn (internal R&D, **not** an evidence/marketing claim); moves
no conversion (K) or money (M) ledger gate.

## [3.33.0] - 2026-07-12 — S3-mini R&D yield: circulate contract at signature granularity + 2 companion generality fixes

First yield of the S3 real-service-scale greenfield R&D (`dogfood/s3_goal.md`). The S3 StockRoom-mini
calibration burn (a ~40-file TypeScript web service) stopped honestly at the implement-time native-oracle
(typecheck) gate — anti-false-green working as designed — and the stop surfaced **one general class plus two
companion fixes**, all generality-first (no `language ==` in core, no framework literal in core logic),
red-before-green, with the native oracle remaining the only judge. Design ruling:
`dogfood/fable5_reply_2026-07-12_s3-interface-contract.md`.

- **Cross-artifact signature coherence (the general class): "convergence granularity ≤ circulated contract
  granularity."** Independently-generated units converge on a shared emergent surface only to the granularity
  of the contract information that circulates in the generation/repair loop. Producer *names* circulated (so
  consumers converged on them), but producer *signatures* were delivered nowhere — so consumers each re-invented
  the shape of a shared interface (e.g. one test called `handle.request({...})` while the producer declared
  `request(method, path, options)`), and the repair loop oscillated instead of shrinking. Fix: raise the
  delivery granularity to match the implement prompt's existing "signatures … VERBATIM" promise, without any
  new artifact/node/gate.
  - `codd/llm/templates/plan_derive_meta.md`: pin the semantics of a task's `dependencies` as the build/run
    **import/include/invoke** edge (including the in-process harness a test drives), not merely a conceptual
    "follows" edge. Template change auto-invalidates the plan-deriver prompt cache.
  - `codd/implementer.py` (`_dependency_artifact_files_context`): add a third producer source that measures the
    consumer's **actual on-disk imports** and reverse-looks-up the owning task (accurate at repair time, where
    the oscillation lives). Two-tier dependency-artifact budget with a **distance-1 signature floor** invariant —
    a consumer prompt always carries its distance-1 producer surface at signature granularity or above; on
    overflow the tier degrades to signatures (never below) and emits budget telemetry (no silent cap).
  - `codd/implement_oracle_scope.py` / `codd/implement_oracle.py`: new `render_public_surface` signature-level
    renderer (extension-dispatched; a language without a renderer preserves prior behavior), wired into both the
    dependency-artifact degradation path and the exporter-surface repair feedback. Ladder:
    signature → names → paths → None.
- **TypeScript module-specifier coherence contract** (companion; mirrors the v3.31.0 C# namespace contract):
  under NodeNext/Node16, independently-generated files split on the relative-import `.js` extension convention
  (some emit `./x.js`, some `./x`) → TS2835. Profile-declared `imports.module_specifier_guidance` resolved
  through `resolve_module_specifier_guidance` and injected at both the generation and implement prompt stages;
  the native typecheck stays the enforcing gate.
- **Runtime-dependency manifest completeness** (companion): the SUT legitimately imports a spec-permitted
  third-party runtime package but the generated manifest declared only the test toolchain → TS2307. Added a
  runtime-dependency-declaration obligation to the implement prompt (`resolve_runtime_dependency_guidance`,
  data-projected from the toolchain profile's manifest filename — no package/framework hardcode in core), so the
  model declares the runtime packages it imports; the existing lock-refresh/materialize pipeline consumes them.

The StockRoom-mini burn is an internal calibration run (R&D), **not** an evidence/marketing claim, and moves no
conversion (K) or money (M) ledger gate.

## [3.32.1] - 2026-07-11 — Docs: Top-6 greenfield campaign CUT (all six languages ②b green)

Documentation-only release marking the Top-6 Language Greenfield Campaign complete. With v3.32.0
the sixth and final language (C#) produced an unattended `codd greenfield` SUCCESS from the neutral
multi-module calculator spec — joining Python, TypeScript, JavaScript, Java, and C++. The campaign
is CUT (Fable5 final-cut ruling, `dogfood/fable5_reply_2026-07-11_final-cut-and-s3.md`).

- **READMEs (en/ja/zh): language-coverage note updated** to all six languages, stating the
  asymmetry honestly (TS/Python ≥2-of-3 independent green; JS/Java/C++/C# n≥1), the
  execution-based validation (native test-report cross-check), the honest-stop-not-false-pass
  guarantee, and an explicit non-claim of enterprise-scale complexity (deferred to the follow-on
  real-spec campaign).
- No production code changed. The owner-packet items (①red-escalation thresholds, ④deep-collapse
  deferral) were owner-delegated to Fable5 and ruled — ① shipped in v3.31.0, ④ deferral ratified as
  a documented limitation.

## [3.32.0] - 2026-07-11 — C# verify no longer clobbers its own TRX report (the last csharp ②b blocker)

The csharp5 re-run cleared implement (the v3.31.0 namespace contract held) and its SUT was
fully green — **97 tests passed, 0 failed** — yet verify FAILED: the dotnet-trx adapter
found `TestResults/test.trx` unparseable ("not parseable XML: line 1, column 2"). The file
contained the `dotnet test` CONSOLE STDOUT ("Determining projects to restore…", the
Passed! summary), not the TRX XML.

Root cause: csharp.yaml's verify `report.capture` was `stdout`, but `dotnet test --logger
"trx;LogFileName=test.trx"` WRITES the TRX file ITSELF — it is a file-writing runner (like
Java surefire and C++ ctest-junit, both `capture: none`), not a stdout-streaming one (like
`go test -json`, which legitimately uses `capture: stdout`). The executor's step (d) —
"if `report_capture == stdout`, persist captured stdout to report_path" — therefore
OVERWROTE the real TRX (written by `--logger` to that exact path) with the console stdout,
so the adapter read non-XML and verify false-red'd on a green SUT.

- **csharp.yaml: verify `report.capture: stdout` → `none`** — a one-line profile DATA fix
  aligning C# with the other two file-writing runners. Red-before-green; verified
  end-to-end (`codd verify` on the actual csharp5 tree now parses the TRX and passes,
  exit 0).

This was the last blocker between csharp's already-green SUT and a clean ②b — with it,
the Top-6 greenfield ② campaign reaches all six languages (Python, TypeScript,
JavaScript, Java, C++, C#).

## [3.31.0] - 2026-07-11 — C# namespace-coherence contract + source-completeness red escalation

Two Fable5 rulings land together (the owner-packet items are Fable5-delegated per the
2026-07-09/11 owner directives — the owner is never the gate).

**C# namespace-coherence contract** (csharp4 stop-loss, exception cycle 3). The csharp4
re-run died in implement with `module_resolution_error ×34`: independently-generated
files split on the namespace convention — impl declared `namespace ExprCalc.Evaluator;
public static class Evaluator` (a namespace segment sharing a TYPE's name), so tests
under `using ExprCalc;` resolved `Evaluator` to the NAMESPACE and every call failed
CS0234. The C++ sibling (v3.30.0) could make the BUILD tolerant of both conventions;
namespace shadowing is language semantics, so C#'s convention must be pinned at
GENERATION instead:

- **`resolve_namespace_guidance`** (`codd/languages/contract.py`, exported): the profile's
  `imports.namespace_guidance` prose, `{package_name}`-substituted — the same
  data-driven convenience seam as `resolve_test_framework_guidance`. `None` ⇒ append
  nothing (only csharp.yaml declares it today).
- **csharp.yaml** declares the contract: every first-party file uses exactly
  `namespace {package_name};` (file-scoped), no sub-namespaces, never a namespace
  segment sharing a declared type's name. The enforcing gate is the native oracle.
- Injected at BOTH prompt stages: generate (`_resolve_layout_placement_contract` rides
  the pre-rendered placement string — design docs are where a namespace convention is
  first written down) and implement (`_build_implementation_prompt`, alongside the
  import-coherence/layout contracts).

**Source-completeness RED escalation** (the ① owner-packet item, thresholds
data-derived per the default-values policy). The `source_completeness` DAG check
escalates amber → red / deploy-blocking when the discovery gap is SYSTEMIC — BOTH
`missing >= 5` (small-project ratio-spike guard) AND `missing/on_disk >= 0.5`. The
defaults never hit a measured-healthy state (②-green fleet max: java 10/31 = 32.3%,
where the toolchain still executes the inert files); knobs
`dag.source_completeness.red_min_missing`/`red_min_ratio`, either 0 disables.
The ④ deep-collapse item is DEFERRED by the same ruling (documented limitation;
revisits with S3 real-spec scale or the first real project that trips it).

Both red-before-green; the namespace contract is verified rendering against the actual
csharp4 tree config.

## [3.30.0] - 2026-07-11 — The scaffolded cpp test target resolves both intra-tree include conventions

The v3.28.0 cpp re-run reached the deepest point of any C++ run — the repair loop engaged
with two-sided (note-attached) feedback and every earlier class stayed fixed — and exposed
the next, final ambiguity: independently-generated e2e test files disagreed on the
quoted-include convention for the SAME same-tree helper (three used file-relative
`"helpers/expr_fixtures.h"`, two used root-relative `"tests/e2e/helpers/expr_fixtures.h"`),
and the scaffolded CMakeLists resolved only the file-relative form → one honest
`module_resolution_error`.

- **`cpp.yaml`: the scaffolded test target declares the repo root as a PRIVATE include
  dir** (`target_include_directories(<name>_tests PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")`),
  so BOTH legitimate conventions resolve and the ambiguity class is moot — a deterministic
  scaffold DATA fix, not an LLM-behavior nudge. PRIVATE: the repo root never leaks into the
  library's usage requirements. Red-before-green; verified against the actual cpp4 dogfood
  tree (the one-line change takes the generated project from the include red to a 100%
  compile-and-link of the full test binary).

Milestone recorded alongside: the java4 re-run on v3.28.0 completed the full unattended
pipeline (elicit→…→verify 46/46 VBs execution-verified→check) — **Greenfield autopilot:
SUCCESS** — Java joins Python/TypeScript/JavaScript in the ②b column.

## [3.29.0] - 2026-07-11 — A transcription that fails the native oracle is not a transcription (csharp stop-loss cycle 2)

The csharp3 re-run walked further than any prior C# run (implement fully green through the
v3.27.0 authenticity fix) and died in **verify**: the F7.1 test re-derivation (T2) drew an
immutability test that ASSIGNS to read-only properties — CS0200, a test that does not even
compile — and the machinery misread the compile-red fresh verify as "a real impl/design
defect or an unconverged transcription", burning the per-task budget on an invalid draw.

- **`test_rederivation.py`: native-oracle acceptance for re-derived tests** (opt-in
  `oracle_check`; the greenfield pipeline wires a one-shot `run_implement_oracle_gate`).
  A draw whose result fails the oracle with SUT-fixable findings is an INVALID
  transcription: it gets ONE diagnostics-informed completion retry within the SAME draw
  (the budget is not re-claimed — a retry is a completion, not a second claim); still
  failing → an honest RED that names the oracle. An environment-only red (zero-infra
  clause) or a finding-less failure never rejects — the fresh verify stays the backstop,
  so this can never manufacture a false-RED or mask one (verify still gates).
  Red-before-green; verified against the actual csharp3 tree (the CS0200 draw is rejected
  with 6 SUT-fixable findings).

## [3.28.0] - 2026-07-11 — Diagnostic notes + qualified-return helpers (cpp/java stop-loss cycle 2)

The v3.27.0 cpp re-run proved the linker fix live (attempt 1 parsed 8 `missing_symbol`
findings and the repair loop engaged instead of aborting) and then exposed the next
recognition gap in the same family: the repaired `src/error.cpp` collided with a header
that defines the same members inline — 7 `redefinition of …` errors, each followed by
g++'s `note: 'X' previously defined here` naming the OTHER side (`include/…/error.h`).
Notes were skipped wholesale ("a warning/note is not a failure"), so the repair feedback
carried only the .cpp side of a two-sided disagreement and the rerun could not converge
("signature unchanged at broad rerun — stopping").

- **`oracle_cpp.py`: a positioned `note:` following an error is now ATTACHED to that
  finding** — appended to its message and its path joined to `failed_paths` — so repair
  feedback names both sides of the disagreement (redefinition site + previous-definition
  site). An orphan note (no preceding finding) stays non-finding noise, and notes are
  capped at 2 per finding (overload-candidate spam adds no repair signal). Red-before-green;
  verified against the actual cpp3 dogfood build output (7/7 findings carry the header-side
  note; both files attributed).

The java3 re-run equally proved v3.27.0 live (the clamped pom compiled; the oracle chain
went green) and exposed the recognizer-family's Java instance: a SAME-FILE
`private static Expr.Literal assertLiteral(…)` helper — whose body runs real
`assertInstanceOf`/`assertEquals` — was invisible to `_JAVA_METHOD_DEF_RE` because the
return-type pattern allowed only a single identifier, so three delegated assertions
false-redded as `unresolved_helper` at the marker-authenticity gate.

- **`vb_marker_authenticity.py`: Java method definitions with QUALIFIED (dotted) return
  types (`Expr.Literal`, `Map.Entry<K, V>`) are now visible** to the helper-resolution
  def-finder, so the hop-0 (same-file) delegated-assertion path resolves them.
  Red-before-green; verified against the actual java3 dogfood test file (3/3 flagged
  tests now `helper_resolved`).

## [3.27.0] - 2026-07-11 — ②b smoke stop-loss: three recognition/projection classes (java/cpp/csharp all-red triage)

The fresh Java/C++/C# exprcalc greenfield smokes all terminated red on **current-core v3.26.0** —
each a distinct *recognition/projection* class, none an architecture-level novelty (ruling:
`dogfood/fable5_ruling_2026-07-11_smoke-stoploss.md`). All three fixes are red-before-green
tested and verified against the actual failing dogfood artifacts.

- **Scaffold defaults are now host-toolchain-clamped** (`scaffold.host_version_clamps`,
  java.yaml + `project_types.py`) — the environment-continuation projection of toolchain
  materialization (v3.15.0 lineage) applied to the generic-template scaffold: a profile may
  declare a version probe (`javac -version`) for a defaults key; when the probed host major is
  LOWER than the declared default, the scaffolded value clamps to the host (min semantics —
  declared 21 on a JDK-17 host scaffolds 17; declared 11 stays 11). Probe failure fails OPEN
  (declared value unchanged); a clamp is never silent (named in the scaffold detail). Without
  this, the scaffolded pom demanded `--release 21` on a JDK-17 host and the whole run collapsed
  (java2 dogfood). Core stays language-free: probe argv + pattern live in the profile YAML.
- **GNU ld diagnostics now parse** (`oracle_cpp.py`) — `undefined reference to `sym'` (with or
  without the `/usr/bin/ld:` prefix / two-line `in function` form) becomes a `missing_symbol`
  finding carrying the SYMBOL identity, deduped per (TU, symbol); the `collect2:` epilog is a
  parse-side summary only and deliberately NOT benign on its own (anti-false-green). Before, a
  pure link failure had no parseable diagnostic, was mis-synthesized as `environment_build_error`,
  and `_only_environment` aborted the repair loop (cpp2 dogfood: header-declared accessors with
  no definitions — the repair loop never saw the 31 missing symbols).
- **Positionless `Fatal error compiling:` now parses** (`oracle_java.py`) — parity with the C#
  adapter's positionless `error CS####` catch: the maven-compiler-plugin fatal (e.g.
  `error: release version 21 not supported`), which carries no `File.java:` anchor and hid
  inside the `Failed to execute goal …` summary-echo, becomes an `EVIDENCE_OTHER` finding
  (message-deduped across maven's wrapped + bare echo forms) instead of an opaque env red.
- **C# generic asserts are recognized** (`vb_marker_authenticity.py`) — the marker-authenticity
  assertion detector's regex required `(` immediately after the method name, so EVERY
  generic-parameterized assert (`Assert.IsType<T>(…)`, `Assert.Throws<T>(…)`,
  `Assert.Equal<int>(…)`, nested up to depth 3) was invisible: a real 5-assertion test was
  reported as "test with NO assertion" and the bounded authenticity rework ran dry (csharp2
  dogfood false-red, 9→4→1). The same change closes the inverse false-green: a generic
  constant-only `Assert.Equal<int>(1, 1)` previously fail-opened to `direct` credit; it now
  screens as `constant_direct`.

Known deferred limitation (documented in the ruling): the scoped-rerun derivation layer
(`implement_oracle_scope.py`) remains TS-only — non-TS oracle failures still rerun broad. The
abort root-cause fixed here was the env misclassification, not the broadness.

## [3.26.0] - 2026-07-11 — Funnel-floor: two reported CLI-robustness crashes (#33, #28)

The first sprint after the v3.25.0 backlog publish closes two reported crashes on
hand-authored / edge-case input. Both follow the same convergence doctrine — normalize
the malformed-input *class* at one shared seam, never a per-symptom guard.

- **#33 — `codd scan` no longer crashes on null `depends_on` / `depended_by`.** A bare
  `depends_on:` / `depended_by:` frontmatter key parses to `None`; the raw
  `.get(key, [])` + iterate raised `TypeError: 'NoneType' object is not iterable`
  (live from v2.19.0 through v3.25.0). Both loops now route through the existing
  None-safe `frontmatter.as_list` accessor and skip non-dict entries (malformed shape
  the validator already reports), so a scan never crashes on hand-authored frontmatter.
- **#28 — `codd propagate-from` no longer crashes on `date:` frontmatter.** A propagated
  design doc whose frontmatter carries a bare `date: 2026-05-29` (PyYAML → `datetime.date`)
  raised "Object of type date is not JSON serializable" when the propagation log serialized
  it. A new shared date/datetime-safe JSON serializer (`codd/json_safe.py`) is now used at
  the log write. (v3.25.0 fixed the propagation *context* path; this completes the *log* path.)

Both fixes are red-before-green tested. #27 (noisy AI wave_config output) and #29 (non-TTY
`codd init`) — also reported — were already resolved in the v3.25.0 train and are verified
fixed as of this release.

## [3.25.0] - 2026-07-11 — Greenfield convergence: implement consumes the planner's task-dependency graph (Fable5 ts-v9 ruling, FIX-1–5)

Unattended `codd greenfield` could complete design → plan but then fail during **implement**: the
barrel/facade was generated before the producers it re-exports, and the (B′) producer-content injection
was starved of producer truth. Fable5's ts-v9 diagnosis (`dogfood/fable5_reply_2026-07-10_ts-v9.md`)
found the root cause — implement derived its execution order, injection closure, and repair campaign from
the **design-elaboration DAG** (whose `depends_on` edges point opposite to module imports, so a shallow
public-API barrel outranks the deep producers), while the planner's task-level `dependencies` graph — the
correct production order the planner actually emitted — was consumed by **nothing**. This release makes
implement consume that production graph, validated by re-running unattended greenfield to green.

Every change is graph/data/config-layer; the native-oracle (tsc) gate, the escalation ladder, and the
oscillation honest-stop classifier are byte-identical (anti-false-green preserved). No `language ==` branch
was added to shared core (ratchet: 58, unchanged). 23 red-first tests
(`tests/test_tsv9_task_graph_convergence.py`, `tests/test_tsv9_secondary_fixes.py`), each RED before its fix.

- **FIX-1 — implement consumes the planner task-`dependencies` graph at all three sites.** (a) *Ordering*
  (`_topologically_order_implement_tasks`): primary rank is now the cycle-safe longest-chain over the task
  `dependencies` production graph (a producer precedes every consumer), with the old
  `(design-rank, is_test, index)` key demoted to a tiebreak; an edge-less/legacy plan collapses to the prior
  order byte-identically. (b) *Injection* (`_dependency_artifact_files_context`): the (B′) producer-content
  context is the nearest-first **transitive closure** over the task graph, unioned with the design-closure,
  under the existing budget ladder. (c) *Repair campaign* (`implement_oracle_scope`, greenfield `pipeline`):
  reruns walk the same production rank so producers regenerate before consumers.
- **FIX-2 — symbol-owner repair evidence names the real on-disk exporter.** `symbol_owners_for_diagnostics`
  and the exporter-surface block resolve the actual authoring file (excluding the diagnostics' own implicated
  files and ownerless symbols) so a missing-export repair edits the producer, not the consumer.
- **FIX-3 — rerun scope skips no-authored-artifact tasks.** `_reimplement_tasks` /
  `_rerun_tasks_with_feedback` no longer re-run tasks that author nothing (a construction-time no-op return,
  not a broad fallback), so a doc/gate task can't absorb a producer's rerun.
- **FIX-4 — plan-intake grounding gate (new, default-on).** When a task's `expected_outputs` is prose that
  describes already-authored codebase files, the derive stage performs a bounded re-derivation and, if still
  ungrounded, fails with an honest `StageError` naming the task instead of silently mis-owning. Knob:
  `derive.plan_intake_grounding_max_retries` (default 2; 0 = gate on, no retry).
- **FIX-5 — `impl_step_derive` defaults to the base `ai_command`** (behavior change) and suppresses the
  now-redundant operation-flow-unused warning.
- **Campaign journal** — the implement repair campaign is recorded to
  `<session>/implement_oracle_campaign.yaml` (evidence only; never read to decide green).

**Empirical validation (② bar).** Fresh unattended `codd greenfield --language typescript` on a neutral
spec, with the fixes live, reached `Greenfield autopilot: SUCCESS` on independently-generated plans (ts-v10
and ts-v11; different task/wave shapes), clearing the ≥2-of-3 bar — the `implement_public_api_barrel` + AST +
parser tasks that failed in ts-v9 all completed and verify passed (vitest); a third run honestly stopped at
verify (PARTIAL_SUCCESS, no false-green). **Scope:** unattended greenfield is empirically validated
end-to-end on **TypeScript and Python** today; the other supported languages share the same language-profile
machinery but have not yet had unattended end-to-end validation. This release does not claim universal repair
convergence — FIX-1–5 change implement's *inputs* (order, injection, rerun scope), and the correctness gates
are byte-identical.

This is also the first PyPI publish of the entire 3.11 → 3.25 train (the F1–F7.1 work held unpushed pending
this ② convergence).

## [3.24.1] - 2026-07-10 — F7.1: make test re-derivation actually complete a live draw (evidence ownership + crash containment + fixpoint)

v3.24.0's F7 had NEVER completed a live re-derivation. Given the actual first-firing artifacts, Fable5
found the shipped code resolved a blocked test file's OWNING task via the write-fence path-resolver —
whose config-wide roots `['src','tests']` prefix-own every test file — so ownership was awarded to the
plan-order-first requirements **gate task** (which authors nothing). Implement then ran on that
no-artifact task → "produced 0 generated files" → an unhandled `CoddCLIError` **escaped** the
re-derivation runner → a misleading stage crash that skipped budget accounting, the audit event, and the
shipped honest-RED path. Because a no-output doc task always precedes authoring tasks, this was
deterministic for every greenfield plan; F7 crashed at its first live implement call. Unit tests (mocked
ownership) never exercised it — only the 現物 did.

F7.1 (Fable5-authorized; self-approvable — conformance of the implementation to the already-ruled F7
design, no new concept; language-free):
- **A — evidence-based ownership (root fix).** `owning_task_for_path` now resolves by AUTHORSHIP
  EVIDENCE only, never the write-fence resolver: the test file's first `@generated-from:` header
  (provenance = ground truth) matched to `task.design_node`, then declared evidence
  (`output_paths ∪ expected_outputs`, `fnmatchcase`) ranked exact/glob above dir-prefix; no match →
  `None` (fail-closed → the existing honest terminal). A `_task_declares_no_authored_artifact` task can
  never own a test file. The header parse is comment-prefix-agnostic and language-free.
- **B — crash containment + rollback (honesty fix).** The re-derivation runner loop is wrapped; on any
  exception the write-fence rolls the tracked tree back to its entry snapshot (new
  `_OracleWriteFence.rollback()` — a crashed draw leaves no partial transcription), the task's budget is
  consumed (a crash is not a re-roll), the audit event records the error, and it returns STATUS_RED. The
  escaped-CoddCLIError path is gone; a blocked-test outcome never again terminates as "produced 0
  generated files".
- **C — fixpoint iteration.** `_drive_test_rederivation` is now a bounded loop that re-enters
  re-derivation with the same per-task budget map when the follow-up surfaces fresh `blocked_test_paths`
  for a DIFFERENT task — exiting on GREEN, not-ran, or no-new-blocked-paths. Each running iteration
  consumes ≥1 unspent per-task budget (Σ ≤ max_per_task × |tasks|), so it terminates; the per-task
  budget is the oscillation guard, superseding v3.24.0's hard "one more loop" count (which wrongly capped
  the genuinely-independent-second-broken-test case). No new knob.

Anti-false-green (unchanged): draws-per-oracle ≤ 1/run; every draw impl-blind (no verify output, no SUT
src) through the unchanged VB/authenticity gates; GREEN only via full fresh verify; a genuinely buggy
impl keeps its re-derived test red forever (second claim budget-blocked → honest StageError); the scope
guard is byte-unchanged; B-full stays rejected. Red-first (10 tests incl. replaying the real js-v7
first-firing artifacts — the terminal is never "produced 0 generated files" for a blocked-test outcome).
Full suite 7356 passed / 1 xfailed / 0 skipped. No `language ==`. PATCH.

## [3.24.0] - 2026-07-10 — Impl-blind test re-derivation: arbitration without an arbiter (F7)

After F1-F6, JavaScript greenfields greened only ~1/3 of runs — below the ② bar (≥2/3). Fable5, given the
ACTUAL failure artifacts (repair history + generated src/tests + a green contrast, copied in-repo since
it can't read the run workspaces), overturned the framing twice: there were NO impl bugs and NO
repair-direction defect. All residual failures were ONE class — **generated test assertions that no
design-conforming implementation can satisfy** (tautologies like `expect(false).toBe(true)`, a leaked
TypeScript `never` reducing to `not.toBeInstanceOf(Object)`, and a wrong transcription constant `toBe(4)`
where the design pins 2). The implementations were verify-green-capable in 3 of 3 runs; the tests were
broken transcriptions of the design. The run died because **test-write authority does not exist anywhere
in the verify/repair phase** — repair correctly won't edit a test (anti-false-green), so a defective
transcription is a guaranteed death with budget unspent. Sampling more can't help (arithmetic: ~0.6%
defective assertions × ~170/run ⇒ P(clean) ≈ 36% ≈ the observed rate; sampling estimates a rate, it can't
raise it).

F7 — impl-blind test re-derivation (Fable5, self-approvable; the constructive form of the Inc1-revert
principle "a gate may only fire on defects repairable within the firing phase's write authority"): route
a defective transcription to the phase that HAS test-authoring authority (implement), instead of
dead-ending. There is **no arbiter** — the design remains the only oracle:
- **Trigger, deterministic, no LLM verdict in routing.** T1: when the auto-scope-guard rejects a
  proposal and EVERY offending path is a test file under a `verify_contract_not_green` failure (mixed
  proposals stay hard terminals), the loop threads a structured `blocked_test_paths` into
  `final_status.yaml`. T2: a new optional `test_defect_claim` in the propose schema + one propose-prompt
  rule — "if an assertion cannot be satisfied by ANY design-conforming implementation, do NOT patch the
  test; emit the claim (checked by re-derivation, never trusted)"; a claim-only proposal is a structured
  terminal, not an engine-failure strike. This removes the perversity that reporting a broken test
  required attempting a forbidden edit.
- **Route (`codd/greenfield/test_rederivation.py`, new).** On a qualifying outcome, for each blocked
  path gated by: it maps to a derived-task output, it bears the codd generation header (**a header-less
  human-authored test is NEVER re-derived — brownfield safety**), and the task's re-derivation budget is
  unspent (`repair.test_rederivation.max_per_task`, default 1) — re-run ONLY the owning tasks' tests-spec
  under an impl-blind feedback ("re-derive EVERY expected value strictly from the design + VB contract;
  carry over nothing"), write-fenced to the test paths, re-check the unchanged implement-side gates
  (coverage + VB marker-authenticity), then GREEN only via a fresh verify. Records
  `<session>/test_rederivation.yaml`. Modeled on the shipped `_drive_vb_authenticity_rework`. Default-ON
  (unlike the reverted Inc1, this adds no new red source — its worst case is one wasted fenced rerun at a
  currently-guaranteed-death point; opt-out `repair.test_rederivation.enabled`).
- **F7b (subordinate) — evidence-complete on the contract path.** v3.23.0's F3/F5 shipped but the primary
  greenfield contract path (`_tuple_from_execution`) never fed them: it now surfaces per-test failure
  entries + captured assertion text (windowed) and runs B0 attribution as READ-ONLY evidence, ending the
  RCA hallucinations. Evidence-only — it does not alter routing (preserves `verify_contract_not_green`).

Anti-false-green (Fable5-verified): the re-derivation prompt is conditioned on the ORIGINAL authoring
distribution MINUS the SUT — design closure + VB contract + (B′) dependency-producer files + the current
test file — and NEVER the owning task's src bodies, NEVER verify observations. So no information can flow
from a buggy impl into the regenerated test; the false-green channel does not exist structurally. Copying
the old broken assertion keeps the run RED (a convergence miss, not a correctness miss). The scope guard
is byte-unchanged; repair still cannot edit tests; GREEN is decided solely by fresh verify. No
`language ==`. Red-first (9 DoD tests incl. replaying the two real failing runs as fixtures → trigger →
re-derivation → green). Full suite 7346 passed / 1 xfailed / 0 skipped. Self-approvable (Fable5-authorized,
exercising the delegated designer-reserved arbitration authority). Expected JS green rate after F7 ≈ 0.9,
over the ≥2/3 ② bar. MINOR.

## [3.23.0] - 2026-07-09 — Auto-repair: budget-gated, evidence-complete convergence (F1-F6)

With the infrastructure failures fixed, dogfood runs began failing verify on genuine generated-impl bugs
that auto-repair should fix but abandoned — e.g. a JavaScript `evaluate()` returning `undefined` for
every input (11/17 integration tests failing "expected undefined to be 14"), left unrepaired with the
loop reporting PARTIAL_SUCCESS / `ALL_REMAINING_UNREPAIRABLE_OR_PRE_EXISTING` while attempt budget
remained. Fable5 root-caused (and authorized the fix — owner delegated all decisions): **the repair
loop's termination was judgment-gated, not budget-gated.** A failing test-run collapses into ONE
`test_command` violation, so a single per-round open-world judgment — an engine exception, an LLM
meta-classifier ruling the failure "broad mismatch → unrepairable" (which perversely abandons the
BROADEST, clearest bugs first), a "no-patch" proposal, or a malformed diff failing `git apply --check`
twice — ended the WHOLE loop with budget unspent. This is the same class the Inc1 revert named: an
open-world question ("is this repairable?") was being JUDGED terminally instead of STEERED.

Six language-blind fixes (no `language ==`; anti-false-green untouched — tests stay read-only, the scope
guard is unchanged, GREEN is still decided solely by the post-repair verify; F1-F6 only grant more
attempts and more evidence, never a green path):
- **F2 — engine failure is a strike, not a verdict** (`repair/loop.py`): a propose/apply exception
  consumes the attempt and RETAINS the violation; only after N consecutive strikes on the same violation
  key (`repair.engine_failure_strikes`, default 3) is it ruled unrepairable. Turns a one-hiccup death
  into up to the full budget of real attempts.
- **F1 — observed ⇒ repairable, deterministically** (`repair/repairability_classifier.py`): an observed
  `test_command`/`typecheck_command` failure that is not `environment_build_error` is unconditionally
  repairable — the `code_addressable`/non-empty-paths preconditions (which gate addressing HINTS, not
  repairability) are dropped, removing the LLM meta-classifier and its inverted "broad mismatch" rule
  from the observed-failure path and closing the no-adapter hole (mocha / `node --test`). D3
  (`environment_build_error` → unrepairable) is preserved and still runs first.
- **F3 — evidence-complete propose** (`repair/loop.py`, `llm_repair_engine.py`, `schema.py`,
  `templates/propose_meta.md`): the picked failure's `error_messages` and the read-only evidence files
  (attribution `evidence_nodes`, previously discarded at coercion) are threaded into the propose prompt
  in an explicitly IMMUTABLE section — the expected-vs-received value + the test's call shape is the
  localization signal for a missing-`return` facade.
- **F4 — deterministic diff→full-file escalation** (`templates/repair_strategy_meta.md`): after a
  unified-diff validation failure the retry MUST use `full_file_replacement`; the "no-patch" trapdoor is
  removed from the retry menu (a no-patch becomes an F2 strike, never a terminal exception).
- **F5 — wider evidence window** (`repair/verify_runner.py`): repair failure reports keep head+tail
  (first 2000 + last 10000 chars) instead of tail-4000, so evidence from many failing tests survives.
- **F6 — status honesty** (`repair/loop.py`): PARTIAL_SUCCESS now requires non-empty
  `applied_patch_files`; a zero-patch terminal is REPAIR_FAILED, not "partial success".

Also sets the **② acceptance bar** (Fable5): per language, ≥2 of 3 fresh unattended single-runs GREEN,
every non-green ending in an honest terminal reason with zero infrastructure-class failures (matching the
Python 3× precedent) — measured only AFTER these fixes land. Red-first (9 tests); 8 pre-existing
multi-violation tests updated to the new strike/status semantics. Full suite 7333 passed / 1 xfailed / 0
skipped. All F1-F6 self-approvable (Fable5-authorized, existing-architecture-conformant). MINOR.

## [3.22.2] - 2026-07-09 — Generalize the CLI-e2e-modality downgrade to all languages (K3 was Python-only)

The v3.20.0 K3 fix (downgrade `e2e_modality` "cli"→"none" when the CLI-backing `runnable-entrypoint`
surface is excluded, so a pure-library project generates no CLI-invoking e2e tests) was silently
Python-only: only the Python layout profile declared a `runnable-entrypoint` `SurfaceSpec` (with
`backs_e2e_modality="cli"`). The TypeScript builder and the generic language synthesizer (cpp, csharp,
java, javascript) declared NO optional surfaces at all — so `effective_e2e_modality` had nothing to key
on and never downgraded. Every pure-library NON-Python greenfield therefore kept `cli` and generated a
full CLI-invoking e2e suite (`invokeCli`, `tempWorkspace`, `cli-usage.e2e.test.ts`, …) against a CLI that
was correctly never scaffolded → verify hard-failed. Confirmed live: a TS pure-library greenfield failed
verify on `tests/e2e/helpers/invokeCli.ts`.

General fix (no per-language stopgap, per the owner's generality directive): a shared constructor
`_runnable_entrypoint_surface()` bakes the invariant `id="runnable-entrypoint"` +
`backs_e2e_modality="cli"` in ONE place; the Python builder now calls it (byte-identical), and the
TypeScript builder + the generic synthesizer (cpp/csharp/java/js and any future greenfield-synthesis
stack) now declare the runnable-entrypoint surface through it. Non-Python entrypoint surfaces carry
`paths=()` (their scaffolders materialize no single canonical entry file), so `excluded_surface_paths()`
stays `()` and scaffolding/placement-contract rendering is byte-neutral — the surface is purely the
e2e-modality marker + plan-intake classification target. Go (no bridge) resolves to `None`, a correct
no-op. A registry-driven guard test asserts EVERY buildable layout profile that declares a
runnable-entrypoint surface backs `"cli"`, so a newly-added language auto-inherits the invariant and
cannot silently regress. `effective_e2e_modality` itself is unchanged (only per-profile DATA was missing);
no `language ==` added; full suite 7324 passed / 1 xfailed / 0 skipped.

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
