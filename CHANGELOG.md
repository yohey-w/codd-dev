# Changelog

All notable changes to CoDD are documented in this file.

## [1.18.0] - 2026-05-05

### Added

- **`codd validate --screen-flow` 強化** (cmd_359, commit eb6a298):
  filesystem_routes が空のときに silent return するのをやめ、`CoddCLIError` を昇格。
  error message に configured `base_dir` 値を含めて debug 容易化。
  greenfield 初期 (screen-flow.md 不在) は warning level で許容を維持。
- **`codd coverage --screen-flow-threshold`** (cmd_359):
  既存 e2e / design_token / lexicon に並んで `screen_flow_coverage` metric を追加。
  CI gate で screen-flow drift をブロックできる。
- **CoverageAuditor `auth_ui_surface` セクション** (cmd_360, commit 0e2238b):
  LMS checklist に `ux:auth:signin` / `ux:landing:root` / `ux:auth:signup` の
  3 entry を追加。OWASP / コンプラ偏重で UX surface (login画面 / root landing /
  signup) が抜け落ちる従来の盲点を解消。
- **`codd.yaml [ux] required_routes` override**:
  framework / SDK で異なる auth route 名 (NextAuth `/auth/signin`, Clerk `/sign-in`,
  nuxt-auth `/auth/login` 等) を codd.yaml で project ごとに上書き可能に。
  `KnowledgeFetcher.suggest_ux_required_routes()` は **defaults のみ** 提示し、
  CoDD core に framework 固有名をハードコードしない設計。
- **`codd implement` screen-flow.md injection** (cmd_361, commit 885799c):
  implementation_plan.md に加えて `docs/extracted/screen-flow.md` (or
  `docs/screen-flow.md`) を AI prompt に注入。UI route ('/login' / '/' 等) の
  task が page 生成リストから漏れる従来の問題を解消。
- **UI task 検出 + 0-file generation ERROR**:
  task description に `page`/`screen`/`login`/`route`/`view`/`widget`/`画面`/`ログイン`
  等のキーワードが含まれる task を `_is_ui_task()` で判定。生成ファイル数が 0 で
  `skip_generation: true` 未指定の場合は `CoddCLIError` を昇格 (silent pass を排除)。

### Notes

- 後方互換: 既存 CLI (drift / validate / propagate / fix / implement / coverage / deploy 等)
  はすべて変更なし。新挙動はすべて opt-in or 安全側。
- 既存テスト 821 (v1.17.0) → 852 (v1.18.0)、+31 件追加、全件 PASS / 0 FAIL / 0 SKIP
  (Python 3.12 .venv + tree_sitter installed)。
- Generality Gate: knowledge_fetcher の AUTH_UI_PACKAGES / implementer の
  _FRAMEWORK_KEYWORDS は依存検出列挙 (cmd_343 で確立した汎用検出パターンと同思想)、
  CoDD core の中核ロジックには framework 固有名を焼き込んでいない。

## [1.17.0] - 2026-05-05

### Added

- **`codd deploy`** (cmd_354): `deploy.yaml`-driven deploy CLI with plug-in target registry
  - `--target TEXT` / `--config FILE` / `--apply` (default dry-run) / `--rollback` /
    `--healthcheck-timeout SECONDS`
  - **VPS Docker Compose target** (commit 03f1baf): SSH + git pull + docker compose pull/up
    で deploy。snapshot は git rev-parse HEAD、rollback は git checkout で復元
  - **Azure App Service target** (commit e975b31): az CLI 経由で webapp deploy + slot swap
    で snapshot/rollback
  - healthcheck (HTTP GET + timeout + retries) で deploy 後検証、失敗時 auto rollback
  - `@register_target` デコレータで新規 target plug-in 追加可能 (cmd_344 strategy registry と同思想)
- **`codd propagate --reverse`** (cmd_345, commit 04c6b2c): DESIGN.md / lexicon 変更を git diff
  で検知し Coherence Engine の DriftEvent として発行
  - `--source [design_token|lexicon]`: 変更検知対象
  - `--base TEXT`: 基準 git ref (default: `HEAD~1`)
  - `--apply`: safe な置換のみ apply (default は dry-run)
- **`codd require --propagate`** (cmd_346, commit 9b7bbcb + 1ee4048): requirements.md frontmatter
  変更を CEG `depends_on` 逆走で関連設計書に反映
  - `--base TEXT`: 基準 git ref (default: `HEAD~1`)
  - `--apply`: AI-generated update proposals を該当ファイルに適用 (default は dry-run)
  - CEG `find_depended_by(node_id)` で逆走、循環検出付き
  - propagator `_build_update_prompt` を再利用、AI 呼出経路は既存と統合
- **`codd validate --screen-flow`** (cmd_347, commit c4616e9): screen-flow.md の routes と
  filesystem routes (codd.yaml `filesystem_routes`) を比較して drift を検出。Coherence
  Engine と統合し、`screen_flow_drift` kind として DriftEvent を発行可能 (opt-in
  `set_coherence_bus()` フック)
- **`codd coverage`** (cmd_348, commit 0401d28): E2E カバレッジ + design-token カバレッジ +
  lexicon コンプライアンスを統合した merge gate
  - `--e2e-threshold FLOAT` (デフォルト 100.0): E2E 網羅率の閾値
  - `--lexicon-threshold FLOAT` (デフォルト 100.0): lexicon 準拠率の閾値
  - `--json`: 機械可読 JSON 出力 (CI 連携向け)
  - exit code 1 on threshold failure (CI gate 利用想定)

### Backward Compatibility

- 既存 CLI (`codd drift` / `validate` / `propagate` / `fix` / `implement` / `verify`) はすべて変更なし
- 新フラグ (`--reverse` / `--propagate` / `--screen-flow` / `--coverage` / `deploy`) は opt-in
- 既存テスト 747 (v1.16.0) → 821 (v1.17.0)、+74 件追加、全件 PASS、regression なし

### Notes

- Coherence Engine (v1.16.0-alpha + v1.16.0) の双方向伝搬パスが本リリースで完成。
  forward = `propagate` / reverse = `propagate --reverse` / requirements → design = `require --propagate` / deploy = `codd deploy` で
  CoDD の整合性駆動が「設計→実装→デプロイ」を一気通貫に統合。
- Generality Gate: deploy_targets / require_propagate / propagator に framework 固有コードゼロ。
  Docker / Azure / SSH は plug-in 内に閉じ、core は target 抽象 (`DeployTarget` インターフェース) のみ。

## [1.16.0] - 2026-05-04

### Added

- **`codd fixup-drift`** CLI: Coherence Engine 検知後の自動修正コマンド
  - `--dry-run` (デフォルト): 修正提案を diff/text 形式で表示のみ、本流無傷
  - `--apply`: git worktree で隔離して適用 (失敗時は worktree 削除で本流無傷)
  - `--severity [red|amber|green|all]`: 処理対象の severity フィルタ
  - `--kind [url_drift|design_token_drift|lexicon_violation|screen_flow_drift|all]`: drift kind フィルタ
- **Fix Strategy plug-in registry**: `@register_strategy` デコレータで kind→Strategy を登録、`list_registered_kinds()` で確認可能
- **`UrlDriftFixStrategy`** (`codd/fixup_drift_strategies/url_drift.py`): URL drift は破壊的変更を伴うため **HITL only** (pending_hitl.md に記録)
- **`DesignTokenDriftFixStrategy`** (`codd/fixup_drift_strategies/design_token_drift.py`): 大小文字統一など safe な正規化は auto-apply、値変更/削除は HITL
- **`LexiconViolationFixStrategy`** (`codd/fixup_drift_strategies/lexicon_violation.py`): lexicon 違反 (用語規約・circular dependency 等) は **HITL only**
- 全 strategy は dry-run でも HITL 候補を `docs/coherence/pending_hitl.md` に追記し、人間レビュー導線を確保

### Notes

- 後方互換: 既存 CLI (`codd drift` / `validate` / `propagate` / `fix`) はすべて変更なし
- v1.16.0-alpha (Coherence Engine 中央ハブ) と本リリースで Phase 1〜4 が一貫完成
- alpha リリースは別 tag (`v1.16.0-alpha`) として歴史保存

## [1.15.0] - 2026-05-04 (retroactive)

### Added

- **`codd e2e-generate`** CLI: screen-flow.md + requirements.md から Playwright/Cypress
  テストスタブを自動生成 (`--framework playwright|cypress` で切替、`--base-url`/`--output` 指定可)
- **`ScenarioExtractor`** (`codd/e2e_extractor.py`): screen-flow と requirements を解析し、
  routes / actions / acceptance_criteria を `docs/e2e/scenarios.md` に出力
- **`TestGenerator`** (`codd/e2e_generator.py`): scenarios.md の各 UserScenario から
  `.spec.ts` (Playwright) / `.cy.ts` (Cypress) ファイルを `docs/e2e/tests/` に生成
- design token / lexicon ヒントを生成テストに自動注入 (Coherence Engine 連携)
- Generality Gate 適合: Next.js / React / Vue / Svelte / Flutter 等フレームワーク非依存
  (Markdown パース + パス操作のみ、特定 FW の import なし)

### Notes

- Phase commits: a686374 (ScenarioExtractor) + 73bc7e7 (TestGenerator + CLI)
- このリリースは **retroactive**: v1.16.0-alpha が先に release 化されたため、tag は
  73bc7e7 に逆打ちされている。pyproject.toml は HEAD で 1.16.0a0 のまま (タグ時点のスナップショット参照)。

## [1.16.0-alpha] - 2026-05-04

### Added

- **Coherence Engine** (`codd/coherence_engine.py`): DriftEvent 統一フォーマット +
  EventBus (in-process pub/sub) + Orchestrator
  - severity ルーティング: `red` → auto-fix dispatch / `amber` → pending HITL 記録 / `green` → log
  - auto-fix 失敗時 amber 自動ダウングレード (payload に `auto_fix_error` / `downgraded_from` 記録)
  - ntfy 通知レート制限 (デフォルト 60 秒クールダウン、`ntfy_rate_limit_seconds` で設定可)
  - severity と fix_strategy の直交化、event 個別 override + codd.yaml `[coherence] routing` で override 可能
- **Coherence Adapters** (`codd/coherence_adapters.py`):
  drift / validation / design-token violation 出力 → DriftEvent 変換アダプター
  - `codd/drift.py` / `codd/validator.py` に opt-in `set_coherence_bus()` hooks 追加
  - 既存出力フォーマット (`DriftResult` / `ValidationResult` / `DesignTokenViolation`) は維持
- **Propagator Coherence Injection**: `codd propagate --coherence` フラグで lexicon と
  DESIGN.md を AI プロンプトに注入 (用語ぶれ・色値矛盾の自動防止)
- **Fixer Coherence-Mode**: `run_fix(coherence_event=...)` で DriftEvent 入力時のみ
  設計書修正を許可 (test 失敗修正フローは従来通り、入口分岐で完全分離)

### Backward Compatibility

- 既存 CLI (`codd drift` / `validate` / `propagate` / `fix`) は全件動作不変。
  `--coherence` / `coherence_event=...` を渡さない限り従来挙動を完全維持。
- 既存テスト 636 件 → 679 件 (+43 件 Coherence 系) すべて PASS、regression なし。

### Notes

- ⚠️ alpha 版: Phase 4+ (Detector ↔ Applier の直接配管 / `codd fixup-drift` サブコマンド) は
  cmd_344 以降で実装予定。本リリースはアーキテクチャ確立段階。

## [1.14.0] - 2026-05-04

### Added

- `codd implement --max-tasks N` option (default: 30): abort if plan contains more tasks than limit
- `codd implement --wave WAVE_ID` option: execute only tasks in the specified wave
- Preflight task count guard with actionable error message (wave/max-tasks/task options)

## [1.13.1] - 2026-05-04

### Fixed

- **DesignTokenDriftLinker**: `__init__` で `project_root` を `Path` に変換していなかったため
  CLI から文字列パスを渡すと `TypeError` が発生するバグを修正 (commit 90f016f)

## [1.13.0] - 2026-05-04

### Added

- **DesignMdExtractor** (`codd/design_md.py`): DESIGN.md (Google Stitch OSS, W3C Design Tokens
  spec) を YAML front matter としてパースし、design_token ノード・参照 edge を生成
- **lexicon design_token vocabulary**: `codd/templates/lexicon_schema.yaml` に `design_token`
  標準カテゴリ (color/typography/spacing/component) を追加
- **KnowledgeFetcher UI framework detection**: React/Vue/Svelte/Flutter/SwiftUI/Jetpack Compose
  を `detect_tech_stack()` で検出し、DESIGN.md spec を lexicon draft に提案
- **DESIGN.md prompt injection** (`codd implement`): `.tsx/.jsx/.vue/.svelte/.swift/.kt/.dart`
  生成時に DESIGN.md トークンを AI prompt に自動注入 (未存在時は warning + skip)
- **`codd validate --design-tokens`**: UI ファイル内の #hex リテラル / px 値を DESIGN.md
  トークン集合と照合し、ハードコードを violation として報告
- **DesignTokenDriftLinker** (`codd drift`): UI 実装で使用されるトークン参照と DESIGN.md
  定義を比較し design_token drift を検出
- **`codd verify --design-md`**: `npx @google/design.md lint` を呼び出し lint 結果を CoDD
  レポートに統合 (npx 不可の場合は skip + warning)

### Notes

- Generality Gate 準拠: UI 言語非依存 (Web/Mobile/Desktop 汎用。React 特化なし)
- DESIGN.md 未存在プロジェクトでも CoDD は通常動作 (warning のみ、強制しない)
- 新規 unit test 22 件追加 (合計 629 PASS / 0 SKIP)

## [1.12.0] - 2026-05-04

### Added

- **`ProjectLexicon` — meta-design context layer** (`codd/lexicon.py`, `codd/templates/lexicon_schema.yaml`) — declare project vocabulary (node types, naming conventions, design principles, failure modes, extractor registry) in `project_lexicon.yaml`. All CoDD commands (`codd require`, `codd plan`, `codd generate`, `codd implement`) now auto-inject lexicon context into AI prompts.
- **`KnowledgeFetcher`** (`codd/knowledge_fetcher.py`) — Web Search-first knowledge layer. Fetches framework/language-specific knowledge at runtime with 30-day cache (`.codd/knowledge_cache/`). Returns `KnowledgeEntry` with `provenance` / `confidence` / `fetched_at` fields. Eliminates hardcoded framework knowledge from CoDD core.
- **Lexicon provenance/confidence fields** — every lexicon entry now carries `provenance` (`web_search` / `official_doc` / `human` / `inferred`), `confidence` (0.0–1.0), and `fetched_at` (ISO 8601). Entries with `confidence < 0.6` emit a warning in `as_context_string()`. Out-of-range values raise `LexiconError` at load time.
- **`codd validate --lexicon`** — new subcommand (`codd/validator.py`) that checks naming convention references within `project_lexicon.yaml`. Reports unknown conventions as violations.
- **Extractor registry** (`codd/registry.py`) — dynamic loader: declare extractor classes by Python module path in `project_lexicon.yaml` `extractor_registry:` block. `load_extractor()` / `get_extractor()` / `list_extractors()` API. `FileSystemRouteExtractor` (v1.11.0) is the first registry entry.
- **Lexicon wizard in `codd plan`** (`_ensure_lexicon()`) — when `project_lexicon.yaml` is absent, `codd plan` auto-generates a draft file (`provenance=inferred`, `confidence=0.5`) using `KnowledgeFetcher.detect_tech_stack()`. Draft includes detected tech stack context and a reminder to confirm before relying on it.
- **`CoverageAuditor`** (`codd/coverage_auditor.py`) — requirement gap detection with 3-class rule: `AUTO_ACCEPT` (industry-standard / legal mandate, recorded silently), `ASK` (project-specific trade-off, presented to human), `AUTO_REJECT` (clearly out of scope, recorded in `scope_exclusions`). Confidence < 0.85 forces `ASK`. Outputs `docs/requirements/coverage_audit_report.md`.
- **`context_acquisition` section in `codd.yaml.tmpl`** — `strategy: web_search_first`, `cache_ttl_days: 30`, `fact_check.min_confidence: 0.6` template defaults.
- **Lexicon question template** (`codd/templates/lexicon_questions.md`) — 21 structured questions across 8 categories (URL/route naming, DB/model naming, environment variables, CLI, roles/permissions, events, components, modules) to guide project lexicon creation.

### Notes

- Generality Gate: no framework-specific knowledge hardcoded into CoDD core. All domain knowledge is fetched at runtime or declared in `project_lexicon.yaml`.
- Full regression: 598 passed / 0 failed / 0 skipped (`.venv`). Pre-existing `tree_sitter` / `synth_templates` failures remain in system Python only.

## [1.11.0] - 2026-05-04

### Added

- **`FileSystemRouteExtractor`** — convention-driven extractor for filesystem-routing frameworks (Next.js App Router/Pages, SvelteKit, Nuxt 3, Astro, Remix). Declared in `codd.yaml` `filesystem_routes:` block. Generates endpoint nodes from directory structure with dynamic segment / group / parallel route normalization.
- **`DocumentUrlLinker`** — scans design/requirement node text for URL strings via regex and auto-creates edges to matching endpoint nodes. Format-agnostic (Mermaid, ASCII art, prose). Configured via `codd.yaml` `document_url_linking:` block.
- **`codd drift` command** — set-difference between design-referenced URLs and implementation endpoints. Reports design-only / impl-only drifts with closest-match suggestions. Exit code 1 on drift for CI integration.
- **`codd extract --layer routes`** — reverse-extracts filesystem routes into Mermaid screen-flow diagrams. Role-based subgraph splitting via URL-prefix inference. Useful for brownfield projects to bootstrap design docs from existing implementations.
- **Filesystem Routing Adapter Recipes** — README section with 5 ready-to-use codd.yaml examples for Next.js / Remix / SvelteKit / Nuxt 3 / Astro.

### Notes

- Validated on osato-lms (large Next.js codebase): endpoint nodes 1 → 109, drift detection 186 cases. See cmd_334 report.
- Generality Gate enforced: no framework-specific hardcoding in `routes_extractor.py` or `drift.py` core. All conventions declared via `codd.yaml`.

## [1.10.0] - 2026-04-19

### Added

- **Multi-language support in `codd implement`** — `implementer.py` now respects `project.language` from `codd.yaml` instead of hardcoding TypeScript. Added `LANGUAGE_EXT_MAP` with 12 languages (TypeScript, JavaScript, Python, Rust, Go, Java, Kotlin, Swift, C++, C, C#, Ruby). `LANGUAGE_ALIASES` normalizes shorthand variants (`ts`, `py`, `rs`, `golang`, etc.). Language-specific comment prefixes (`// @generated-by:` for C-family, `# @generated-by:` for Python/Ruby) and code fence markers also applied automatically. TypeScript remains the default fallback for backward compatibility. Resolves [#12](https://github.com/yohey-w/codd-dev/issues/12).

### Fixed

- **`codd implement` excludes failed task summaries from downstream prompts** — failed task output is no longer injected into subsequent phase prompts, preventing error noise from propagating through the implementation pipeline.

## [1.9.0] - 2026-04-16

### Added

- **Multi-AI engine support for `codd implement`** — file-writing agents (Codex) detected automatically via `_is_file_writing_agent()`. Git-based change capture: baseline → run agent → `git diff` → format as `=== FILE: ===` blocks → revert. Claude interactive mode (without `-p`) also supported as file-writing agent.
- **Automatic parallel execution within phases** — tasks grouped by phase number (`m1.x`, `m2.x`). Same-phase tasks run concurrently via `ThreadPoolExecutor` (max 4 workers). File-writing agents use git worktree isolation to prevent conflicts. Stdout agents parallelize without overhead.
- **Phase milestone parser** — `#### M1.1 Title（period）` format extracted from `## Milestones` section. Takes priority over Sprint and legacy Milestone formats.
- **`_group_tasks_by_phase()`** — groups `ImplementationTask` list by phase number for parallel scheduling.
- **`_execute_task()`** — extracted single-task execution into reusable function.
- **`_execute_phase_parallel()`** — orchestrates concurrent execution with worktree isolation for file-writing agents.
- **`_create_worktree()` / `_remove_worktree()`** — git worktree lifecycle management for parallel Codex execution.

### Changed

- AI command timeout increased from 600s to **3600s** (1 hour) for heavy reasoning models (e.g., GPT-5.4 xhigh).
- `implement_tasks()` now processes phases sequentially with intra-phase parallelism by default. No flag needed.
- `_invoke_ai_command()` accepts `project_root` kwarg to route file-writing agents.

## [1.8.0] - 2026-04-14

### Added

- **Diagnostic reasoning step in `codd fix`** — AI must now produce a `## Diagnosis` section identifying the root cause *before* writing any code fix. Prevents blind trial-and-error patching.
- **Session state persistence across retries** — `_SessionState` accumulates prior attempt history (diagnosis, approach, outcome) and injects it into subsequent retry prompts as `## Prior attempts (DO NOT repeat these)`. Eliminates repeated failed approaches.
- **`diagnosis` field on `FixAttempt`** — extracted root cause diagnosis is stored per attempt for downstream analysis and reporting.

### Changed

- `_build_fix_prompt()` now enforces a two-step workflow: Step 1 (Diagnose) → Step 2 (Fix). Retry prompts include full session history.
- `run_fix()` loop creates `_SessionState` and records each failed attempt before retrying.

### Performance

- **SWE-bench Verified**: 73/73 instances resolved (100%) with diagnostic reasoning + session state, up from 93.3% (28/30) without these features.

## [1.6.0] - 2026-04-06

### Added

- **OSS/Pro split** — `reviewer`, `verifier`, `audit`, `risk` modules moved to `codd-pro` private package
- Entry-points based plugin discovery (`codd.plugins` group) — `require_plugins.py` now uses `importlib.metadata.entry_points`
- Bridge pattern in `validator.py` and `policy.py` — Pro implementations override OSS fallback when `codd-pro` is installed
- `bridge.py` — central plugin registry for Pro extensions
- `codd-dev[ai]` optional dependency group for `extract_ai.py`
- `codd-dev[mcp]` optional dependency group for `mcp_server.py`
- Graceful degradation for `review`/`verify`/`audit`/`risk` commands — shows migration message when `codd-pro` is not installed

### Removed

- `codd/reviewer.py`, `codd/verifier.py`, `codd/audit.py`, `codd/risk.py` — moved to `codd-pro`

### Migration

Users who rely on `codd review`, `codd verify`, `codd audit`, or `codd risk` should install `codd-pro`:
```
pip install "codd-pro @ git+ssh://git@github.com/yohey-w/codd-pro.git"
```
All other commands (`scan`, `generate`, `propagate`, `extract`, `validate`, `require`, `restore`, `plan`, `measure`, `impact`) are unaffected.

## [1.5.1] - 2026-04-06

### Fixed

- `codd measure` crashed with `TypeError: 'dict' object is not callable` — `ceg.nodes` is a dict attribute, not a method ([#3](https://github.com/yohey-w/codd-dev/issues/3))
- `codd validate` falsely reported `conventions.targets` nodes as "undefined" even when they existed in `nodes.jsonl` — validator now loads scan results into the known-node lookup ([#4](https://github.com/yohey-w/codd-dev/issues/4))
- `codd extract` → `codd plan --init` failed on brownfield projects because `codd.yaml` was never created — extract now auto-generates a minimal `codd.yaml` when none exists ([#2](https://github.com/yohey-w/codd-dev/issues/2))

## [1.2.1] - 2026-04-01

### Fixed

- `codd hooks install` failed with FileNotFoundError after `pip install codd-dev` — hooks/pre-commit was excluded from the wheel package ([#1](https://github.com/yohey-w/codd-dev/issues/1))
  - Moved `hooks/` into `codd/hooks/` package so it's included in wheel builds
  - Converted `codd/hooks.py` to `codd/hooks/__init__.py` package

## [1.2.0] - 2026-03-31

### Added

- `codd plan --waves` and `--sprints` flags — return counts for shell scripting (no hardcoded magic numbers)
- `codd-assemble` skill for Claude Code integration
- Assembler prompt improvement for cleaner output

### Changed

- README overhauled: split Quick Start into Greenfield/Brownfield, added 5-Minute Demos, articles section

## [0.2.0a5] - 2026-03-29

### Added

- **`codd extract` — Brownfield bootstrap from existing codebases**
  - Reverse-engineers CoDD design documents from source code using static analysis
  - No AI required — pure deterministic structural fact extraction
  - Philosophy: in V-Model, intent lives only in requirements; everything below
    is structural fact that static analysis can extract
  - Supports Python, TypeScript, JavaScript (full import + symbol extraction),
    Go (symbol + import extraction), Java (symbol extraction only; import tracing planned)
  - Two-phase architecture: extract-facts (static analysis) → synth-docs (templated Markdown)
  - Auto-detects language, source directories, test directories, frameworks, ORMs
  - Generates `system-context.md` (module map + dependency graph) and per-module
    design documents with full CoDD frontmatter
  - Module cards include: classes, public functions, internal/external dependencies,
    file list, test mapping, detected patterns (API routes, DB models)
  - Confidence scores capped below green band — human review always required
  - Works without `codd init` (true brownfield: no prior CoDD setup needed)
  - Output to `codd/extracted/` as draft documents; promote after review

## [0.2.0a1] - 2026-03-29

### Public Alpha Release

First public alpha of CoDD (Coherence-Driven Development). Core graph engine
and impact analysis are stable. Generation and verification are experimental.

### Added

- **V-Model verification phases** aligned with IPA Common Frame
  - Unit tests verify detailed design, integration tests verify system design,
    E2E tests verify requirements
  - Test strategy derived from architecture (no manual configuration)
- **Derivation principle**: upstream docs + best practices = downstream is self-evident
- `codd verify` command with V-Model loss function
- `codd implement` command for design-to-code generation
- `codd plan --init` for automatic wave config generation from requirements
- `codd generate` with AI-driven document content generation
- `codd validate` for frontmatter and dependency integrity checks
- `codd hooks install` for Git pre-commit integration
- Detailed design wave support (Wave 4.5)
- Prior task context injection to prevent code duplication in implementation

### Changed

- **Renamed CPDD to CoDD** (Coherence-Driven Development)
- Migrated graph store from SQLite to JSONL for portability
- Frontmatter is now the Single Source of Truth (graph data in codd/scan/ is a derived cache)
- README rewritten for competitive positioning against Spec Kit / OpenSpec

### Fixed

- Windows path normalization for cross-platform support
- Meta-commentary and AI artifact stripping in generated documents
- Wave config forward references no longer cause false errors (BLOCKED, not ERROR)
- Selective purge preserves human-authored evidence on scan refresh

### Core Commands (Stable)

| Command | Status |
|---------|--------|
| `codd init` | Stable |
| `codd scan` | Stable |
| `codd impact` | Stable |
| `codd validate` | Alpha |

### AI Commands (Experimental)

| Command | Status |
|---------|--------|
| `codd generate` | Experimental |
| `codd verify` | Experimental |
| `codd implement` | Experimental |
| `codd plan` | Experimental |

## [0.1.0] - 2026-02-15

### Initial Release (Internal)

- CEG (Conditioned Evidence Graph) with JSONL-backed dependency graph
- `codd init`, `codd scan`, `codd impact` CLI commands
- Frontmatter-first architecture
- Convention-aware impact propagation with Green/Amber/Gray bands
- Multi-agent operation guide (Shogun system integration)
