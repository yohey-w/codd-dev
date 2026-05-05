# Changelog

All notable changes to CoDD are documented in this file.

## [Unreleased]

## [1.27.0] - 2026-05-06

### Added — Auto-Repair Layer + Polyglot Suffix Support (cmd_398 + cmd_402 + cmd_403)

cmd_393 (declarative) → cmd_392/393 (検証) → cmd_397 (実行) に続く第 4 層として、
**verification 失敗 → LLM 分析 → 試行修復 → 再 verify ループ** (cmd_398) を追加。
さらに、Web/TypeScript 専用の暗黙仮定を解消する polyglot suffix support (cmd_402)、
重複 default catalog yaml の統合 (cmd_403) を同 release で吸収。

「declarative + 検証 + 実行 + 自己修復」で coherence の閉ループが完成し、CoDD は
Agent 性を獲得する。殿哲学「静的ガイドライン < 動的試行修復」が core 実装される。

### Auto-Repair Layer (cmd_398)

#### 5 components (codd/repair/)

- `RepairEngine` ABC + `@register_repair_engine` decorator (engine.py)
- `LlmRepairEngine` — analyze + propose_fix + apply の default 実装 (llm_repair_engine.py)
- `RepairLoop` — max_attempts 制御 + approval_mode 統合 + history 永続化 (loop.py)
- 5 dataclass schema (VerificationFailureReport / RootCauseAnalysis / RepairProposal /
  ApplyResult / RepairLoopOutcome、schema.py)
- CLI 拡張 (`codd verify --auto-repair` / `codd repair --from-report` /
  `codd repair history` / `codd repair approve`)

#### サポート module

- `git_patcher.py` — D-1=A unified diff primary + C full_file fallback (git apply --3way)
- `history.py` — `.codd/repair_history/{ISO8601}/attempt_N/` 永続化 (D-4)
- `verify_runner.py` — Python import 経由の verify 再実行 (D-3、subprocess fork ゼロ)
- `approval_repair.py` — cmd_397_h approval.py を repair 用に extend
- `templates/analyze_meta.md` / `propose_meta.md` — domain-neutral LLM prompt

#### approval_mode (二重 opt-in)

- `required` (default): 各 attempt に HITL approval 必須
- `per_attempt`: attempt ごとに ntfy + inbox
- `auto`: `require_explicit_optin: true` 必須 + `max_files_per_proposal>5` で required にエスカレート

#### cmd_397 抽象の流用

`codd/llm/invoke.py` (cmd_397_f) + `codd/llm/approval.py` (cmd_397_h) を import 利用、
重複実装ゼロ。codd/repair/ は subprocess + JSON + git apply + DAG kind 判定のみ。

### Polyglot Suffix Support (cmd_402 + cmd_402b)

- `IMPLEMENTATION_SUFFIXES` / `TEST_SUFFIXES` を defaults yaml + project override に移管
- 9 言語 default yaml 追加: `rust` / `ruby` / `swift` / `kotlin` / `csharp` / `cpp_embedded` /
  `elixir` / `scala` / `generic` (全主要言語包含 fallback)
- `_detect_project_type` で 10+ marker 自動判定 (Cargo.toml / Gemfile / package.json /
  go.mod / .csproj / CMakeLists.txt / build.gradle / mix.exs / build.sbt / *.swift)
- 4 段 resolution chain: codd.yaml → defaults/{type}.yaml → generic.yaml → LEGACY (warning)
- `implementation_suffixes_extend` / `test_suffixes_extend` で base + extend pattern サポート
- **Java vs Kotlin 判別 fix (cmd_402b)**: build.gradle 単独 → java、build.gradle + *.kt → kotlin、
  pom.xml → java の優先順序で正しく分岐

これにより Rust / Ruby / Swift / Kotlin / C# / C++ / Elixir / Scala project でも CoDD が
impl_file node を生成し、C1-C7 全 check が動作する。**「CoDD = Web/TypeScript 専用」誤解を
構造的に脱する**。

### Means Catalog Consolidation (cmd_403)

cmd_397 で発生した重複 default catalog yaml (codd/llm/templates/ と codd/deployment/defaults/
の 2 箇所に同一内容) を統合。`codd/llm/templates/verification_means_catalog.yaml` のみが
正規 path として残り、`codd/deployment/providers/verification/means_catalog.py` の
DEFAULT_CATALOG_PATH を更新。

### osato-lms 実証

- 意図的に auth design_doc の runtime constraint を破壊 → C7 FAIL 3 件検出
- RepairLoop → REPAIR_SUCCESS (attempts=1)
- C1-C7 全 PASS regression
- `.codd/repair_history/2026-05-05T22:36:24.836307Z/attempt_0/` に永続化
- D-1=C full_file_replacement fallback path も実証 (3way merge 失敗時の挙動確認)

### Generality Gate 二層 (継続)

- layer A (codd/repair/*.py): zero hit (claude/openai/cookie/oauth/nextauth/safari/iphone/
  chromium/powershell/react/smoke/headless/osato-lms)
- layer B (codd/repair/templates/*.md): zero hit
- means catalog 重複も解消 (cmd_403)

### 新 node kind: 0 / 新 edge kind: 0

cmd_393 で確立した「新次元 = attribute schema 拡張」パターンを継続。本 release でも
auto-repair / polyglot を新 kind なしで実装。

### Backwards compatibility

- `codd.yaml [repair.*]` / `[ai_commands.repair_*]` 不在 → `codd verify --auto-repair` はエラー、
  既存 codd verify 挙動不変
- `codd.yaml [implementation_suffixes]` 不在 → 既存挙動 (web defaults or auto-detect)
- 既存 v1.26.0 ユーザは挙動変化ゼロ (cmd_398 関連は完全 opt-in、cmd_402 は既存挙動継承)

### 1763 tests PASS / SKIP=0

v1.26.0 baseline 1708 tests → 現状 1763 (+55 from cmd_398 b/c/d/lms + cmd_403 等)。

## [1.26.0] - 2026-05-06

### Added — CDP-Browser E2E Verification + LLM Test Pipeline (cmd_397)

cmd_393 で declarative に書ける `user_journeys` を **実ブラウザで自動実行**する
verification template (CdpBrowser) を追加。さらに、設計書に書かれていない暗黙の
検証留意点を LLM が動的に補完する LLM test consideration pipeline を追加。
「declarative + 実行 + LLM 補完」で coherence の UX 軸が一周する。

### 新 node kind: 0 / 新 edge kind: 0

cmd_393 で確立した「新次元 = attribute schema 拡張で吸収」パターンを継承し、
本 release でも CdpBrowser engine + LLM 補完層をすべて既存 schema 上に乗せた。

### 新 verification template: cdp_browser

`codd/deployment/providers/verification/cdp_browser.py` に新 plug-in 追加 (cmd_392d
verification template registry を流用)。3 軸 plug-in で stack 多様性を吸収:

- `BrowserEngine` (Edge / Chromium / Firefox / WebKit) — `@register_browser_engine`
- `CdpLauncher` (PowerShell / shell / external_running / WebDriver) — `@register_cdp_launcher`
- `FormInteractionStrategy` (React state setter / 標準 input event / Vue / Angular) — `@register_form_strategy`

CoDD core にはどれも **bundle されない**。cookbook (`docs/cookbook/cdp_browser/`) で
コピペ用テンプレート提供のみ。

### 標準 AssertionHandler 3 種 (CoDD core 同梱)

- `expect_url` (location.href の startsWith / contains / regex 検証)
- `expect_browser_state` (cookie / localStorage / sessionStorage の存在/値検証)
- `expect_dom_visible` (querySelector + 表示判定)

これらは journey.steps の標準語彙であり stack 中立として core bundle。

### CDP wire 最小実装 (websocket-client 依存追加)

`codd/deployment/providers/verification/cdp_wire.py` に websocket + JSON-RPC 最小 client。
依存は `websocket-client` のみ (~50KB)。method 名は launcher / strategy plug-in が指定し、
core は generic な request/response router として動作 (Chrome / Edge / Chromium 中立)。

### LLM Test Consideration Pipeline (cmd_397_f-h)

`codd/llm/` に新 module 一式:

- `LlmConsiderationProvider` ABC + `@register_llm_provider` decorator
- subprocess + ai_command 抽象 (codd/extract_ai 既存 pattern 流用、SDK 依存ゼロ)
- META_INSTRUCTION_TEMPLATE (domain-neutral) + means_catalog hint slot
- 出力 schema validator (cmd_393 user_journeys schema 準拠)
- HITL approval gate (`required` / `per_consideration` / `auto` の 3 mode)
- `verification_strategy` field 追加 (LLM が engine 選択も導出)

### Means Catalog (cmd_397_i)

`codd/llm/templates/verification_means_catalog.yaml` に 6 ドメイン default hint:
web_app / mobile_app / desktop_app / cli_tool / backend_api / embedded。

3-stage fallback resolution:
1. `project_lexicon.yaml [verification_means_catalog]` (project override、最優先)
2. `codd.yaml [llm.verification_means_catalog_path]`
3. CoDD core default

catalog 固有名 (Appium / WinAppDriver) は **YAML hint** として許容、core code は
yaml 内容で if 分岐しない (Generality Gate layer B)。

### 新 CLI

- `codd dag run-journey <journey_name>` (cmd_397_e、journey 単発実行)
- `codd llm derive [--design-doc <path>] [--force]` (cmd_397_f、考慮事項生成)
- `codd llm approve <design_doc> [--consideration <id>] [--all]` (cmd_397_h、HITL 承認)
- `codd llm list [--design-doc <path>]` (cmd_397_h)

### Generality Gate 二層化

- layer A (code): `codd/llm/*.py` + `codd/deployment/providers/verification/*.py` に
  framework / stack 固有名 zero hit (`expect_browser_state` 内の標準 target 語彙
  cookie/localStorage/sessionStorage は journey schema の一部として例外)
- layer B (hint yaml): catalog 固有名 OK、ただし core code が yaml 内容で
  if 分岐しないことを AST 検査で保証

### osato-lms 実証 (cmd_397_lms)

osato-lms に `codd.yaml [verification.templates.cdp_browser]` 追加 + cookbook plug-in
(PowerShell launcher / Edge engine / React state strategy) を copy。実ブラウザで
login → /learner/dashboard 動線が自動化された。LLM derive で auth_design.md → 10+
considerations 生成 + HITL approve + C7 merge も実証。

### Backwards compatibility

- `codd.yaml [verification.templates.cdp_browser]` 不在 → 既存挙動 (playwright/curl)
- `design_doc.user_journeys` 不在 → CdpBrowser を選択する path がない
- `codd.yaml [llm.*]` 不在 → `codd llm derive` はエラー、C7 は manual user_journeys のみ
- 既存 v1.25.0 ユーザは挙動変化ゼロ (cmd_397 関連は完全 opt-in)

### 設計の経緯

- cmd_397_g0 (CDP-Browser engine) と cmd_397_g1_llm_design (LLM 補完) を一本化設計
- 殿確定 4 制約 (3 層モデル禁止 / 戦略導出 LLM 動的 / 手段カタログ hint / Generality 二層) を
  設計書に統合
- 11 phases (pre/a/b/c/d/e/f/g/h/i/lms) で並列実装、足軽 5 名 + 軍師 + 家老体制で完走

## [1.25.0] - 2026-05-06

### Added — User Journey Coherence Layer (cmd_393)

cmd_392 v1.24.0 release 直後に発生した実ブラウザログイン失敗
(HTTP 環境 × `__Secure-` Cookie 不整合) を構造的に検出するための C7 check 追加。
C6 deployment_completeness は API/DB レベルのチェーン検証だが、ブラウザ側の
journey 成立条件 (実環境 capability / 実装証跡 / browser 期待値) は捕捉できなかった。
v1.25.0 では **新 node kind / 新 edge kind を 1 つも追加せず**、既存 4 カテゴリ
(design_doc / impl_file / plan_task / expected_value) の attribute schema 拡張のみで
UX レベルの coherence を担保する。

### 新 node kind: 0 / 新 edge kind: 0

- `Generality Gate` 最強水準: CoDD core に NextAuth / Cookie / `__Secure-` /
  SameSite / Chromium 等の固有名はゼロ
- すべての stack 知識はプロジェクトの `design_doc.frontmatter` /
  `project_lexicon.yaml` / `codd.yaml [coherence.capability_patterns]` に declarative
- CoDD core は string match と set 演算のみの宣言的 coherence engine

### 既存 node の attribute schema 拡張

- `design_doc.attributes`: `runtime_constraints`, `user_journeys` (frontmatter passthrough)
- `impl_file.attributes`: `runtime_evidence` (project 宣言の capability_patterns regex 経由)
- `plan_task.expected_outputs`: `lexicon:` / `design:` 接頭辞対応 (produces edge 拡張)
- `expected (lexicon).attributes`: `journey`, `browser_requirements`, `runtime_requirements`
- `runtime_state.attributes`: `capabilities_provided` (deploy.yaml 由来、yaml ルール上書き可)

### 新 check C7 user_journey_coherence

`design_doc (NFR + journey) → impl_file (evidence) → runtime_state (capabilities)
→ expected (browser/runtime requirements) → verification_test (E2E)` のチェーンを
declarative に検証し、8 violation を検知:

- `missing_journey_lexicon`: design_doc が user_journey を宣言したが lexicon entry なし
- `no_plan_task_for_journey`: journey を expected_outputs に含む plan_task 不在
- `no_e2e_test_for_journey`: plan_task → verification_test (E2E) チェーン断絶
- `e2e_not_in_post_deploy`: E2E test が deploy.yaml post_deploy に未統合
- `unsatisfied_runtime_capability`: design_doc.runtime_constraints が要求する capability を
  runtime_state が満たさない (今夜事故の主検出器)
- `impl_evidence_runtime_mismatch`: impl_file の runtime_evidence が runtime capability と矛盾
  (今夜事故の補強検出器)
- `browser_expected_not_asserted`: lexicon の browser_requirements が E2E で assert されず
- `journey_step_no_assertion`: journey.steps に検証 step 不在

C7 violation 検出時は deploy 'INCOMPLETE_JOURNEY' マーク + DriftEvent
(kind=`user_journey_coherence`, severity=red) publish + ntfy critical 送信。

### 新 CLI

- `codd dag verify --check user_journey_coherence` (C1-C6 と同列で plug)
- `codd dag journeys` (design_doc 横断で user_journey 一覧表示)

### osato-lms 実証 (HTTP × `__Secure-` 事故の構造的検出)

- C7 → `unsatisfied_runtime_capability (tls_termination)` + `impl_evidence_runtime_mismatch`
  + `no_plan_task_for_journey` を red で検出
- C6 deployment_completeness は PASS のまま (回帰なし)
- 修復は cmd_394 (将来 release) の範囲、cmd_393 は検出層完成のみ

### Backwards compatibility

- `user_journeys` / `runtime_constraints` 未宣言 design_doc → C7 SKIP (INFO)
- `capability_patterns` 未宣言プロジェクト → `runtime_evidence` 空 → 検出対象外
- 既存 v1.24.0 プロジェクトは挙動変化ゼロ、opt-in で C7 active 化

### 設計の経緯

- 初期設計案 (新 node 3 種 / 新 edge 3 種) は殿により Generality Gate 違反として却下
- 再設計 (本 release): attribute schema 拡張 + 既存 edge 流用のみで等価機能を実現
- 「新次元 = attribute 拡張で吸収」のパターンを確立、cmd_385 個別 detector 量産路線を回避

## [1.24.0] - 2026-05-05

### Added — Deploy Verification Gate (cmd_392)

cmd_390 deploy 事故 (殿ログイン不可) の構造的解決。設計→実装の静的整合 (cmd_386)
だけでは捕捉できない、デプロイ手順書 / runtime state / verification test の
**動的成立条件** を DAG node として表現し、設計→デプロイ→runtime→検証 の
連続性を C6 check で担保する。

### 新 node 3 種

- `deployment_doc`: `DEPLOYMENT.md` / `deploy.yaml` 等のデプロイ手順書
- `runtime_state`: DB schema / seed 状況 / 起動状況など実行時の成立条件
- `verification_test`: smoke test / health check / login E2E など検証テスト

### 新 edge 4 種

- `requires_deployment_step`: design_doc → deployment_doc
- `executes_in_order`: deployment_doc → impl_file (順序付き)
- `produces_state`: impl_file → runtime_state
- `verified_by`: runtime_state → verification_test

### 新 check C6 deployment_completeness

`design_doc → deployment_doc → impl_file → runtime_state → verification_test`
の連続チェーンを traverse し、6 種の violation を検知:

- `missing_deployment_doc`: 設計書が要求する DEPLOYMENT.md 不在
- `missing_step_in_deployment_doc`: deployment_doc に migrate / seed step 記載なし
- `missing_impl_for_step`: deploy step が指す impl_file (prisma/seed.ts) 不在 / Dockerfile に COPY なし
- `state_not_produced`: deploy 実行で runtime_state が生成されない
- `no_verification_test`: runtime_state を確認する smoke test 不在
- `verification_test_not_in_deploy_flow`: smoke test が deploy.yaml `post_deploy` に組み込まれていない

C6 violation 検出時は deploy 'INCOMPLETE' マーク + DriftEvent
(kind=`deployment_completeness`, severity=red) を CoherenceEngine publish +
ntfy critical 送信 (cmd_377 severity classifier 連携)。

### Generality Gate — 3 plug-in

`codd/deployment/providers/` に registry-decorator pattern の 3 plug-in 追加。
CoDD core (`codd/deployment/__init__.py`) には Prisma / Docker Compose / Playwright
等のプロジェクト固有名のハードコードなし。

- **schema_provider** (`@register_schema_provider`):
  - `prisma`: `prisma/schema.prisma` + `prisma/seed.ts` + `prisma/migrations/` 検出
  - 将来: SQLAlchemy / TypeORM / raw SQL plug-in を追加可能
- **deploy_target** (`@register_deploy_target`):
  - `docker_compose`: `deploy.yaml targets.<name>.type=docker_compose` を parse、`compose_file` から起動順序抽出、`steps` / `post_deploy` フィールド対応
  - 将来: Kubernetes / Vercel / Azure App Service / Cloudflare Workers plug-in を追加可能
- **verification_template** (`@register_verification_template`):
  - `playwright`: e2e/login スマートテスト spec 実行
  - `curl`: health endpoint smoke test
  - 将来: k6 / Cypress / Artillery plug-in を追加可能

### Deploy gate integration (cmd_392f)

`_collect_deployment_completeness_gate` を `_run_deploy_gates` に追加 (6 つ目 gate)。
`codd deploy --apply` で C6 が既存 5 gate と並んで実行される。

- C6 violation 時: deploy block + DriftEvent publish + ntfy critical
- `incomplete_chain_report` JSON 形式で deploy log に記録
- `remediation` hint 自動生成 (例: "Add prisma/seed.ts and ensure Dockerfile COPY includes it")

### osato-lms 実証 (cmd_392_lms)

cmd_390 deploy 事故の再発防止を実環境で確認:

- `DEPLOYMENT.md` 新規作成 (migrate / seed / build / smoke test sections)
- `deploy.yaml` 拡張 (`steps` + `post_deploy` フィールドで migrate → seed → build → start → smoke test を順序実行)
- `tests/smoke/login.test.ts` 新規 (curl POST /api/auth/login 検証)
- `Dockerfile.production` に `COPY prisma ./prisma` 追加 (事故原因の seed.ts 欠落解消)
- `codd dag verify --check deployment_completeness` → osato-lms で **PASS**
- VPS 144.91.125.163:3000 root + /login 共に **200**、殿ログイン可能 を確認

### Notes

- 後方互換: `deployment_doc` node 0 件のプロジェクトでは C6 check SKIP with INFO (deploy block しない)
- 既存 `deploy.yaml` (healthcheck のみ、`steps` / `post_deploy` 不在) は旧形式として valid、C6 は SKIP for that target
- 既存テスト 1210 (v1.23.0) → 1319 (v1.24.0)、+109 件 (cmd_392 新規 7 test ファイル)、全件 PASS / 0 FAIL / 0 SKIP
- Generality Gate: framework 固有名のハードコードなし。`codd.yaml [deployment]` override + `defaults yaml` で project 別制御
- 3 要素セット (cmd_376/377/378) との整合: C6 violation は cmd_377 preflight critical 候補、cmd_378 GLPF re-plan trigger
- CoDD 自律性 6 要素 (cmd_376/377/378 agent 自律 + cmd_386 静的整合 + cmd_388 動的連鎖 + cmd_392 deploy 連鎖) で production deployment まで貫通する自律ガード完成

## [1.23.0] - 2026-05-05

### Added — Change-Driven Auto-Propagation Pipeline (CDAP, cmd_388)

cmd_386 (静的整合性 / deploy 前 gate) と対をなす **動的連鎖** 機構。
ファイル変更を起点に impact → propagate → verify → fix → drift の連鎖を
自動実行し、関連テストのみを抽出して走らせる。Claude / Codex / 手動編集の
すべてを CLI 非依存で同じ propagation 経路に集約する。

- **`codd watch`** (cmd_388_pre + cmd_388a, 9d3599e + 857bbfa):
  watchdog ベースのファイル監視 daemon。Linux / macOS / Windows 全対応
  - `codd/watch/events.py`: `FileChangeEvent` dataclass + EventBus 拡張
  - `codd/watch/propagation_log.py`: `.codd/propagation_log.jsonl` ring buffer
  - `codd/watch/watcher.py`: `Observer` 経由のファイル監視 + debounce
  - `--debounce <ms>` (default: 500) で連続変更 throttle、`--background` daemon mode、`--status` 状態確認
- **`codd propagate-from --files <list>`** (cmd_388b, 65204ab):
  変更ファイル群から impact → propagate → verify → fix → drift の連鎖を 1 回実行
  - `codd/watch/propagation_pipeline.py`: 連鎖 step 統合
  - `--source [watch|git_hook|editor_hook|manual]`, `--editor [claude|codex|manual]` で trigger source 識別
  - `--dry-run` で impact 計算のみ実行、fix / log 書き込み skip
  - `propagation_log.jsonl` に各 step 結果を JSON Lines で記録
- **`codd test --related <file>`** (cmd_388c, 120ed5f):
  DAG `tested_by` edge 経由で関連テストのみ抽出 + 実行
  - `codd/watch/test_runner.py`: `FRAMEWORK_RUNNERS` (jest/vitest/pytest/bats/go test 等) 対応
  - `codd/dag/defaults/test_frameworks.yaml` で project ごと override 可能
  - `--dry-run` で実行コマンドを stdout 出力 (CI 統合用)
- **Hook recipes** (cmd_388d, e0ad7b4):
  Claude / Codex / git の各 hook recipe を `codd/hooks/recipes/` に同梱
  - `claude_settings_example.json`: Claude PostToolUse hook (Edit/Write 後に `codd propagate-from` 自動発火)
  - `codex_hook.sh`: Codex post-edit wrapper
  - `git_pre_commit.sh` / `git_post_commit.sh`: git diff --name-only 経由で propagation 発火
  - README に "Hook Integration — Set It Once, Never Think Again" section 追加
- **cmd_391 cleanup** (eaba5e4): cmd_385 残存 drift_linkers 個別 linker 削除
  - `codd/drift_linkers/api.py` (484 LOC) / `schema.py` (262 LOC) / `screen_flow.py` (171 LOC) を削除
  - registry skeleton (`codd/drift_linkers/__init__.py`) と defaults yaml は cmd_386 で rename 流用済のため別 module で維持
  - 計 -917 LOC、責務が cmd_386 DAG 完全性 gate に上位統合

### 思想

- **静的 + 動的の両輪完成**: cmd_386 (deploy 前 gate) + cmd_388 (ファイル変更時連鎖) で真の Coherence-Driven Development が成立。設計→実装の漏れは静的に止め、変更による波及は動的に追従する。
- **CLI 非依存**: Claude / Codex / 手動編集すべてが同じ `codd propagate-from` 経由で連鎖。multi-CLI orchestrator (memory: project_shogun_repositioning) の差別化を強化。
- **テスト局所性**: 全テストを毎回走らせず、変更起点の reverse closure 上のテストのみ実行。CI 時間短縮 + 開発時 feedback loop 高速化。

### Notes

- 後方互換: 既存 CLI (drift / validate / propagate / fix / implement / coverage / deploy /
  e2e-generate / extract / plan / require / preflight / gungi / dag / test 含む既存) はすべて変更なし。
  新フラグ・新コマンドはすべて opt-in。
- 既存テスト 1185 (v1.22.0) → 1210 (v1.23.0)、+25 件 net (+70 cdap tests / -45 cmd_385 obsolete tests via cmd_391 cleanup)、全件 PASS / 0 FAIL / 0 SKIP
- Generality Gate: framework 固有名 (Next.js / NextAuth / Prisma / TypeScript) の CoDD core ハードコードなし。`FRAMEWORK_RUNNERS` + `codd/dag/defaults/test_frameworks.yaml` で project ごと override 可能。
- 3 要素セット (cmd_376/377/378) との整合: `FileChangeEvent` 連鎖は autonomous-by-default の典型実装、cmd_377 preflight critical 候補、cmd_378 GLPF re-plan trigger と接続可能。

## [1.22.0] - 2026-05-05

### Added — DAG Completeness Gate (cmd_386)

設計書・実装ファイル・計画タスク・期待値を単一 DAG (Directed Acyclic Graph) の
node として表現し、`expects` / `imports` / `depends_on` / `produces` / `represents`
の edge で関係を明示する。DAG 上の完全性を 5 種の check で統一検査することで、
cmd_385 個別 detector 路線の責務分散・値整合二重実装・task 非紐付けを構造的に解消する。

- **`codd dag build`** (cmd_386_pre + cmd_386a, 9f4f8de + 74b401e):
  設計書 / 実装 / 計画 / 期待値から node + edge を抽出し `.codd/dag.json` を生成
  - `codd/dag/__init__.py`: DAG / Node / Edge dataclass + Tarjan による cycle 検出
  - `codd/dag/builder.py` + `codd/dag/extractor.py`: 既存 `extractor.py` を流用した node 抽出
  - `--format mermaid` で `.codd/dag.mmd` 可視化出力
- **`codd dag verify --check <name>`** (cmd_386b/c/d/e/f, a00397b/62d2b01/123bf28/6eb1d30/795057d):
  5 種の DAG completeness check を `@register_dag_check` decorator で plug
  - C1 `node_completeness`: design_doc が `expects` する impl_file の存在確認 (severity=red)
  - C2 `edge_validity`: orphan / dangling reference 検出 (severity=red)
  - C3 `depends_on_consistency`: `propagator.py` output を消費し URL/型/値の整合検証 (severity=red)
  - C4 `task_completion`: `plan_task` の expected impl_file 存在 + drift なし判定 (severity=red)
  - C5 `transitive_closure`: root design_doc から leaf impl_file まで unreachable な node 検出 (severity=amber)
- **`codd dag visualize`** (cmd_386g, f792c59):
  Mermaid 形式で DAG を stdout 出力
- **Deploy gate 統合** (cmd_386g, f792c59):
  `_collect_dag_completeness_gate` を `_run_deploy_gates` に追加 (5 つ目 gate)。
  `codd deploy --apply` 時に既存 4 gate (validate / drift / drift_linker / coverage) と
  並んで実行される。red severity FAIL 時は deploy block。
- **DriftEvent integration** (cmd_386g):
  red severity check failure を `DriftEvent (kind="dag_completeness", severity="red")`
  として CoherenceEngine に publish。既存 routing (auto/hitl/manual) と統合。

### 思想転換

cmd_385 (個別 drift detector × 4) → cmd_386 (統一 DAG 完全性 gate) への上位統合。
新しい drift type を追加する際、cmd_385 では linker 1 ファイル丸ごと書く必要があったが、
cmd_386 では check 1 つまたは extractor 拡張のみで済む。既存 `scan` / `impact` /
`propagate` / `CoherenceEngine` を流用し、新規実装を最小化する設計。

osato-lms の「役割別ホーム未実装」(cmd_350 Phase C.5 NG #2) のような task 完了漏れも、
C4 task_completion で構造的に検出可能になる。

### Notes

- 後方互換: 既存 CLI (drift / validate / propagate / fix / implement / coverage / deploy /
  e2e-generate / extract / plan / require / preflight / gungi) はすべて変更なし。
  新フラグ・新コマンドはすべて opt-in。
- 既存テスト 1077 (v1.21.0) → 1185 (v1.22.0)、+108 件追加 (DAG 全 9 test ファイル)、
  全件 PASS / 0 FAIL / 0 SKIP
- Generality Gate: framework 固有名 (Next.js / NextAuth / Prisma / TypeScript) の
  CoDD core ハードコードなし。`codd/dag/defaults/{web,cli,mobile,iot}.yaml` +
  `codd.yaml [dag]` override で project ごと制御。
- cmd_385 残存資産 (`codd/drift_linkers/api.py` / `schema.py` / `screen_flow.py`、
  計 917 LOC) は別タスク (cmd_391 cleanup) で整理予定。registry skeleton と
  defaults yaml は cmd_386 で rename 流用済。
- 3 要素セット (cmd_376/377/378) との整合: dag_completeness 違反は cmd_377 preflight
  critical 候補、re-plan trigger は cmd_378 GLPF medium 級として処理可能。

## [1.21.0] - 2026-05-05

### Added

- **`codd preflight` / `codd gungi`** (cmd_377): autonomous execution preflight checks with
  goal clarity, context completeness, judgment material, and rollback criteria audits.
- Project-type defaults for critical operations under `codd/preflight/defaults/`
  (`web`, `cli`, `mobile`, `iot`) plus `codd.yaml [preflight] critical_operations` overrides.

### Changed

- ntfy delivery is severity-filtered by default: ASK notifications send only for critical
  items, and Coherence Engine HITL notifications send only for red events.

## [1.20.0] - 2026-05-05

### Added — AI-driven Design Artifact Derivation (要件→AI動的設計書群導出)

CoDD の本質的方向性 (memory: project_codd_questioning_ai.md) を具体化する 5 cmd 統合
リリース。テンプレ固定 wave_config を廃止し、要件→AI判断→必要設計書群→wave_config
を動的導出する。HITL 協業型 (recommended 先行進行 + after-fact patch) を初実装。

- **`codd require --completeness-audit`** (cmd_375, commit c52ec80):
  要件文書から設計書導出に必要な情報の欠落を監査し、選択式 ASK を生成
  - `RequirementCompletenessAuditor` + project_type 別 defaults (web / cli / mobile / iot)
  - `AskItem` / `AskOption` を `project_lexicon.yaml:coverage_decisions` に永続化
  - `HitlSession` による HITL 協業型 (`RECOMMENDED_PROCEEDING`) と blocking mode 互換
  - AskUserQuestion / ntfy / lexicon の 3 チャネル送信。Claude 非依存環境では ntfy + lexicon で動作
- **`codd drift --screen-flow`** (cmd_373, commit 7f0010b):
  screen-transitions.yaml と実装コード抽出 transition の双方向差分検知
  - `ScreenFlowDriftResult` dataclass + `compute_screen_flow_drift()` 関数
  - design-only / impl-only / trigger mismatch を `from`/`to` edge 単位で比較
  - DriftEvent (kind=screen_flow_design_drift, severity=amber) として Coherence Engine 統合
- **`codd plan --derive`** (cmd_370, commit c1d7ba9):
  要件文書 + `project_lexicon.yaml:coverage_decisions` から必要設計書群を AI 導出し、
  `project_lexicon.yaml:required_artifacts` に永続化
  - `RequiredArtifactsDeriver` + project_type 別 defaults (web / cli / mobile / iot)
  - `source` は `ai_derived` / `user_override` / `default_template` を必須化
  - cmd_375 の `RECOMMENDED_PROCEEDING` / 確定回答を prompt hint として注入
  - `codd.yaml [project] type` と `[required_artifacts]` override で汎用プロジェクトに適用可能
- **`codd require --audit` required artifact gap detection** (cmd_371, commit 1e92484):
  `project_lexicon.yaml:required_artifacts` と既存 `docs/design/*.md` を比較し、
  AI 導出された必須設計書の欠落を ASK / AUTO_REJECT として監査
  - `ArtifactGap` + `CoverageAuditor.audit_required_artifacts()` / `_discover_existing_artifacts()`
  - `artifact_discovery.paths` / `mappings` / `artifact_paths` による codd.yaml override 対応
  - coverage audit report に `Required Artifacts Audit` セクションを追記し、
    missing artifact の作成先候補と scope exclusion を記録
- **`codd plan --derive --regenerate-wave-config`** (cmd_372, commit c24a868):
  `project_lexicon.yaml:required_artifacts` を topological sort し、`wave_config` を明示フラグ時のみ再生成
  - `generate_wave_config_from_artifacts()` で required_artifacts の `depends_on` から wave を決定
  - 既存 `wave_config` は通常の `codd plan --derive` では変更せず、後方互換を維持
  - 再生成前に `codd.yaml.bak` を作成し、既存設定を保護
  - `codd/codd.yaml.bak` / `.codd/codd.yaml.bak` を gitignore に追加

### 思想転換

wave_config テンプレ固定廃止。要件→AI判断→必要設計書群を動的に決定する仕組みを実装。
**AI は問いつつ進む** (HITL 協業型): ASK 発生 → recommended 値で先行進行 → 殿空き時間で
10 秒判断 → 違ったら after-fact patch。osato-lms screen-flow 不在事故のような「設計書
漏れ」を構造的に再発防止。

### Notes

- 後方互換: 既存 CLI (drift / validate / propagate / fix / implement / coverage / deploy /
  e2e-generate / extract / plan / require) はすべて変更なし。新フラグはすべて opt-in。
- 既存テスト 915 (v1.19.0) → 993 (v1.20.0)、+78 件追加 (cmd_373: 9 + cmd_375: 25 +
  cmd_370: 19 + cmd_371: 11 + cmd_372: 14)、全件 PASS / 0 FAIL / 0 SKIP
- Generality Gate: project_type 別 defaults yaml 分離 (web/cli/mobile/iot 各 4 種、
  required_artifacts と requirement_completeness の両方) + codd.yaml override で汎用適用。
  CoDD core に framework 固有名のハードコードなし。
- HitlSession class は cmd_376 (v1.21.0 候補) で全 CoDD コマンドへの横串改修ベースとなる。

## [1.19.0] - 2026-05-05

### Added — Screen Transition Edge Support (画面遷移エッジ完全対応)

- **`codd extract --layer routes-edges`** (cmd_364, commit 23ed3d1): tree-sitter AST 解析で
  JSX/TSX 内の `<Link href>` / `redirect()` / `router.push()` / `signIn(callbackUrl=...)` /
  `NextResponse.redirect()` 等を抽出し `docs/extracted/screen-transitions.yaml` を生成
  - `codd.yaml [screen_transitions] patterns` で project ごとに override 可能
  - `codd/screen_transitions/defaults.yaml` に Next.js / Nuxt / SvelteKit / Astro / Remix の
    慣例 transition pattern を分離 (CoDD core にハードコードなし)
- **`codd e2e-generate --mode transitions`** (cmd_365, commit 34b3bc7): screen-transitions.yaml
  から `page.goto(from) → click(trigger) → toHaveURL(to)` 形式の Playwright/Cypress
  テストを自動生成。`TransitionTestGenerator` クラス追加
- **`codd validate --screen-flow --edges`** (cmd_366, commit 303a6e2): screen-transitions.yaml と
  screen-flow.md の edge 整合性チェック
  - orphan ルート (in_edges + out_edges = 0) 検出
  - dead-end ルート (out_edges = 0 で end-state でないノード) 検出
  - edge_to_unknown_node 検出
  - `coverage_metrics.compute_edge_coverage` 追加で CI gate に統合
- **`codd drift --e2e`** (cmd_367, commit 9de2aa0): 設計書 transition vs E2E `toHaveURL`
  assertion 差分検知
  - `extract_e2e_have_url_assertions()` で .spec.ts / .cy.ts から URL 到達 assertion を抽出
  - `ScreenTransitionDrift` dataclass + `detect_screen_transition_drift()` 関数
  - DriftEvent (kind=screen_transition_drift, severity=amber) として Coherence Engine 統合
- **implementer wrapper rules** (cmd_368, commit fc61df2):
  - `_is_wrapper_task()` で thin wrapper page (e.g., `src/app/login/page.tsx` が
    `<SignInForm>` を呼ぶだけ) を検出
  - `UI_WRAPPER_PROMPT_RULES` で AI prompt に callback wiring 必須 + spec component 名
    一致 + middleware 単一インスタンス を命令
  - `_check_guard_files_uniqueness()` で `codd.yaml [implementer] guard_files` リストの
    重複ファイル (root middleware.ts と generated/middleware.ts 等) 検出 → CoddCLIError 昇格

### Notes

- 後方互換: 既存 CLI (drift / validate / propagate / fix / implement / coverage / deploy /
  e2e-generate / extract) はすべて変更なし。新フラグはすべて opt-in。
- 既存テスト 852 (v1.18.0) → 915 (v1.19.0)、+63 件追加 (cmd_364: 12 + cmd_365: 16 +
  cmd_366: 13 + cmd_367: 11 + cmd_368: 11)、全件 PASS / 0 FAIL / 0 SKIP
- Generality Gate: framework 固有名 (Next.js/NextAuth/Clerk/Nuxt/SvelteKit) は
  defaults.yaml と codd.yaml override に分離、CoDD core にハードコードなし。
  Playwright/Cypress は e2e_generator の switch 範疇 (cmd_342 で確立した汎用 pattern)。
- cmd_350 Phase C.5 NG #2 (login 後遷移なし) の根本原因 (CoDD のノード only / エッジ非対応)
  を構造的に解消。osato-lms login/page.tsx の callback wiring 漏れも cmd_368 の wrapper
  rules で再発防止。

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
