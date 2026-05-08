# Changelog

All notable changes to CoDD are documented in this file.

## [Unreleased]

## [2.8.0] - 2026-05-08 — LLM-enhanced `codd init --suggest-lexicons` (cmd_456)

### Added

- `codd init --suggest-lexicons --llm-enhanced` invokes the configured AI
  command, passing project requirements / design docs / manifest hints, and
  asks the LLM to recommend lexicons. Each recommendation includes a
  `confidence` (`high` / `medium` / `low`) and a one-line `reason`.
- New `--auto-approve` flag skips the interactive HITL prompt and writes the
  high+medium recommendations directly into `extends:` (CI-friendly).
- `codd/init/llm_lexicon_suggester.py` houses the new dataclasses
  (`LlmLexiconRecommendation`, `LlmLexiconResult`) and the prompt builder.
  `codd_plugins/lexicons/` is enumerated dynamically; the prompt template
  carries no specific lexicon, domain, or compliance literals.

### Behaviour

- Without `--llm-enhanced` the existing regex / `stack_map.yaml` based
  suggestion path stays unchanged (cmd_439, v1.41.0).
- With `--llm-enhanced` and no requirements/design docs to read, or when the
  AI command fails / returns invalid JSON, CoDD falls back gracefully to the
  regex-based suggester instead of erroring.

### Quality Metrics

- **pytest**: 2701 PASS / 0 FAIL / 0 SKIP (no regressions vs v2.7.0)
- **Generality Gate**: zero specific lexicon literal hits in
  `codd/init/llm_lexicon_suggester.py` and the prompt template.
- **Compatibility**: legacy `codd init --suggest-lexicons` (regex mode) still
  works; the new path is opt-in.

## [2.7.0] - 2026-05-08 — Default scope = system_implementation (cmd_455)

### Changed (default behaviour)

- **`DEFAULT_SCOPE` is now `system_implementation` (was `full`).** When a project's `project_lexicon.yaml` does not declare `scope:`, CoDD now suppresses business-concern dimensions (goal/KPI, acceptance/UAT detail, risk register, glossary terms) from `codd elicit` findings. This matches CoDD's design philosophy: it is a coherence verifier for system implementation, not a project-management tool.
- The CoDD core, every example, and the lexicon loader all read `DEFAULT_SCOPE` — no other change was needed.

### Migration

| Project state | New behaviour | Action |
| --- | --- | --- |
| No `scope:` declared | `system_implementation` (filters business dimensions) | none — recommended default |
| Wants legacy "everything" mode | declare `scope: full` in `project_lexicon.yaml` | one-line opt-in |
| PMO / requirements-only review | `scope: business_only` | already supported |

`cmd_445` (scope/phase filter) shipped this filtering machinery in v2.2.0; v2.7.0 only changes which mode is the unspecified default.

### Quality Metrics

- **pytest**: 2697 PASS / 0 FAIL / 0 SKIP (no regressions vs v2.6.1)
- **Generality Gate**: zero specific lexicon literal hits.
- **Compatibility**: explicit `scope: full` keeps the v2.6.x behaviour.

## [2.6.1] - 2026-05-08 — AI command timeout default 30 min (cmd_454)

### Changed

- `DEFAULT_TIMEOUT_SECONDS` (and therefore `resolve_timeout()` when nothing else is set) now defaults to **1800.0 s** (30 minutes) instead of 120 s. Multi-lexicon `codd elicit` pipelines (10+ lexicons at ~30 s each) consistently exceeded the legacy 120 s ceiling. Override paths are unchanged: explicit argument > `CODD_AI_TIMEOUT_SECONDS` env var > `llm.timeout_seconds` in `codd.yaml` > default.

### Quality Metrics

- **pytest**: 2696 PASS / 0 FAIL / 0 SKIP (no regressions vs v2.6.0)
- **Generality Gate**: zero specific lexicon literal hits.
- **Compatibility**: every existing override path keeps its precedence; only the absent-value fallback changes.

## [2.6.0] - 2026-05-08 — `extends` namespace + multi-lexicon auto-load (cmd_453)

> v2.5.0 is reserved for the in-flight `verifies_runtime` binding work
> (cmd_451), which is parked pending a CLI verification cache investigation
> (tracked separately). v2.6.0 ships the namespace and auto-load fixes
> independent of that work.

### Changed

- `project_lexicon.yaml` now uses an `extends:` field for declared lexicon
  plug-ins. The previous `suggested_lexicons:` field still loads — it is
  merged into `extends:` with a `DeprecationWarning`. Existing projects
  keep working without edits.
- `codd init --suggest-lexicons` writes new entries to `extends:` instead
  of `suggested_lexicons:`.
- `codd elicit` consumes the same `extends:` list automatically. With no
  `--lexicon` argument and a non-empty `extends:`, every listed lexicon is
  loaded; with an empty `extends:` the legacy discovery mode runs.
- `--lexicon` now accepts a comma-separated list (`babok,web_responsive`).
  When `--lexicon` is given, it overrides `extends:`.

### Added

- `ElicitEngine.run()` accepts `LexiconConfig | list[LexiconConfig] | None`.
  Multiple lexicons are applied sequentially; duplicate axes are
  deduplicated (first lexicon wins) with a `DeprecationWarning`. Each
  finding now carries a `lexicon_source:` attribute identifying the
  lexicon that detected the gap.
- New tests cover migration shim, CLI auto-load, CSV parsing, and engine
  multi-lexicon paths.

### Quality Metrics

- **pytest**: 2690 PASS / 0 FAIL / 0 SKIP (no regressions vs v2.4.0)
- **Generality Gate**: Layer A (`codd/init/`, `codd/elicit/`, `codd/cli.py`) ships zero specific lexicon literal hits.
- **Compatibility**: legacy `suggested_lexicons:` keeps loading; deprecated only with a warning.

## [2.4.0] - 2026-05-08 — Runtime state auto-binding for deployment chain (cmd_450)

### Added

- `codd_plugins/stack_map.yaml` gains a `deployment_bindings:` block (impl_pattern → runtime_state_kind / runtime_state_target). The mapping itself stays in the plug-in; CoDD core loads it as data.
- `codd.deployment.extractor.load_deployment_bindings()` and `auto_runtime_states_for_impl()` translate auto-discovered impl artifacts (cmd_448) into the runtime_state nodes the deployment chain expects.
- DAG builder now appends those runtime_state nodes alongside the implicit `produces_state` edges so chains like `Dockerfile → runtime:file_present:build_artifact` close themselves without a `codd.yaml` change.
- `_runtime_kind_for_impl` falls back to `deployment_bindings` for anything outside the legacy seed/migration/server set.
- `_verification_matches_runtime` recognises `RuntimeStateKind.FILE_PRESENT` and `ENV_VAR_SET` via keyword targets so smoke checks can match build artifacts/env capabilities.

### osato-lms dogfooding effect

| Stage | unrepairable | broken_at |
| --- | --- | --- |
| v2.2.0 | 16 | `missing_impl_for_step` (Dockerfile) |
| v2.3.0 (cmd_448) | 5 | `state_not_produced` |
| **v2.4.0 (cmd_450)** | **1** | `no_verification_test` (next chain step) |

Chain progression so far: `missing_impl_for_step` → `state_not_produced` → `no_verification_test`. Closing the final step (binding `runtime:file_present:build_artifact` to a smoke verification when the verification target is a URL/path with no shared keyword) is tracked in a follow-up.

### Quality Metrics

- **pytest**: 2680 PASS / 0 FAIL / 0 SKIP (no regressions vs v2.3.0)
- **Generality Gate**: Layer A (`codd/dag/builder.py`) loads bindings as data; the impl_pattern → runtime_state_kind table lives in `codd_plugins/stack_map.yaml`.
- **Compatibility**: legacy projects without `deployment_bindings` continue to work — bindings are optional and additive.

## [2.3.0] - 2026-05-08 — Deployment chain auto-discovery (cmd_448)

### Fixed (surfaced by osato-lms cmd_446 dogfooding)

- `deployment_completeness` no longer emits `missing_impl_for_step` for projects whose deployment artifacts (e.g. `Dockerfile`) live at the standard location but were not enumerated by `dag.impl_file_patterns`. The deployment plug-in now auto-discovers existing impl artifacts that match the deployment doc's section keywords (`migrate` / `seed` / `build` / `start`).

### Added

- `codd.deployment.extractor.discover_deployment_impl_candidates(project_root, deployment_docs)` — generic, plug-in-owned mapping from section keyword to standard filenames; only paths that exist are returned. CoDD core stays free of stack-specific filenames; the mapping lives next to the deployment plug-in.
- DAG builder registers each discovered impl as `kind="impl_file"` with `auto_registered_for_deployment: true`.

### Quality Metrics

- **pytest**: 2680 PASS / 0 FAIL / 0 SKIP (no regressions vs v2.2.0)
- **Generality Gate**: `codd/dag/builder.py` reuses generic glob + existence check; the section→filename mapping lives inside `codd/deployment/extractor.py` (plug-in surface) — Layer A core stays clean.
- **Compatibility**: opt-in via existing impl_file_patterns still works unchanged; auto-discovery only triggers when deployment docs have matching section keywords and the standard files exist.
- **osato-lms dogfooding**: `unrepairable` 16 → 5 (deployment_completeness 2 → 1, verification_test_runtime 14 → 4). Remaining items target the `produces_state` chain and the Vitest matcher, tracked separately.

## [2.2.0] - 2026-05-08 — Elicit bug fixes + scope/phase filter (cmd_445)

### Fixed (P0 release blocker — surfaced by osato-lms cmd_442 dogfooding)

- `codd elicit apply` now respects approval checkbox state (`[x]` → applied, `[r]` → ignored, `[ ]` → pending) instead of dumping every finding into `pending_findings.yaml`.
- `codd extract` / `codd brownfield` no longer crash with `'builtin_function_or_method' object is not iterable` when the schema-design template renders enum values. (`codd/templates/extracted/schema-design.md.j2`)

### Added

- `project_lexicon.yaml` gains optional `scope:` (`system_implementation` / `full` / `business_only`) and `phase:` (`mvp` / `production`) fields. Defaults preserve v2.1.0 behaviour (`full` / `production`).
- BABOK lexicon (`codd_plugins/lexicons/babok/`) annotates each axis with `concern:` (`system` / `business` / `both`); `codd elicit` filters findings by scope and demotes business-tier `high` findings to `info` under `phase: mvp`.

### Quality Metrics

- **pytest**: 2680 PASS / 0 FAIL / 0 SKIP
- **Generality Gate**: `codd/elicit/`, `codd/lexicon.py`, scope/phase paths — zero specific lexicon literal hit (Layer A)
- **Compatibility**: schema additions are additive; existing `project_lexicon.yaml` files load unchanged.

## [2.1.0] - 2026-05-08 — Lexicon coverage CI gate (cmd_443)

### Added

- Added `codd coverage check` for threshold-based lexicon coverage gates with human, JSON, and Markdown output.
- Added configurable `coverage.thresholds` in `codd.yaml` with global, per-lexicon, and per-axis thresholds.
- Added GitHub Actions workflow for coverage matrix artifacts, PR comments, and threshold-gated CI.

### Quality Metrics

- **pytest**: 2663 PASS / 0 FAIL / 0 SKIP
- **Generality Gate**: `codd/lexicon_cli/threshold.py`, `codd/cli.py`, and workflow contain zero specific lexicon literals
- **Compatibility**: default threshold is `0`, so existing projects have no enforcement until they opt in

## [2.0.0] - 2026-05-08 — Lexicon-Driven Completeness milestone (cmd_441)

CoDD v2.0.0 marks a positioning shift, not just a version bump.

### North Star Restated

> "Write only functional requirements and constraints. Code is generated, repaired, and verified automatically."

v1.x delivered the **extract → diagnose → repair** pipeline. v2.0 adds the **constraint side** as a first-class plug-in surface — industry standards (BABOK / WCAG / OpenAPI / OWASP / ISO 27001 / GDPR / Kubernetes / OpenTelemetry / etc.) are now mechanically reusable as coverage axes, not lore living in someone's head.

### Cumulative Changes Since v1.34.0

- **31 lexicon plug-ins across 7 domains** (cmd_438): Methodology / Web / Mobile / Backend-API / Data / Ops / Compliance / Process. Each lexicon ships a manifest, axes, severity rules, coverage matrix, and prompt extension.
- **`codd elicit` (Coverage / Spec Discovery Engine)** (cmd_431, v1.35.0): lexicon-loaded coverage-mode emits gap-only findings; lexicon-less mode keeps backward-compatible discovery.
- **`codd diff` (brownfield drift)** (cmd_436, v1.37.0): three-category classification (implementation_only / requirement_only / drift) with severity coercion.
- **`codd brownfield` pipeline** (cmd_437, v1.38.0): extract → diff → elicit orchestration for existing codebases.
- **`codd init --suggest-lexicons`** (cmd_439, v1.41.0): manifest-file scan → `codd_plugins/stack_map.yaml` regex match → suggested lexicons appended to `project_lexicon.yaml`.
- **`codd lexicon list/install/diff` + `codd coverage report`** (cmd_440, v1.42.0): plug-in management CLI plus JSON / Markdown / self-contained HTML coverage matrices.
- **RepairLoop strategy v2** (cmd_432, v1.35.0): generic fallback chain replaces stack-specific hardcoding.
- **Generality Gate three-layer architecture**: Layer A core ships zero specific framework / domain literals; Layer B templates expose generic placeholders; Layer C plug-ins carry all domain knowledge.

### Quality Metrics

- **pytest**: 2651 PASS / 0 FAIL / 0 SKIP
- **Generality Gate**: zero hit across `codd/elicit/`, `codd/diff/`, `codd/brownfield/`, `codd/init/`, `codd/lexicon_cli/`, `codd/cli.py`
- **Lexicon plug-ins**: 31 (BABOK + 30 across 7 domains), ~280 axes total

### Contributors

- **@yohey-w** — Maintainer / Architect
- **@Seika86** — Sprint regex insight (PR #11)
- **@v-kato** — Brownfield reproduction reports (Issues #17 / #18 / #19)
- **@dev-komenzar** — `source_dirs` bug reproduction (Issue #13)

### Compatibility

- All v1.x CLI commands and config keys remain functional. v2.0.0 adds new subcommands; it removes nothing.
- `project_lexicon.yaml` v1.0 schema is forward-compatible — v1.41.0+ optionally appends `suggested_lexicons:` and existing fields are untouched.

## [1.42.0] - 2026-05-08 — Lexicon CLI + coverage matrix report (cmd_440)

### Added

- Added `codd lexicon list/install/diff` for bundled lexicon plug-in management.
- Added `codd coverage report` for JSON, Markdown, and self-contained HTML lexicon coverage matrices.
- Added lightweight text-grep inspection mode with optional `--with-ai` elicit integration.

### Quality Metrics

- **pytest**: 2651 PASS / 0 FAIL / 0 SKIP
- **Generality Gate**: `codd/lexicon_cli/` + `codd/cli.py` specific lexicon literal zero hit
- **Compatibility**: existing `codd coverage` merge gate remains available without the `report` subcommand

## [1.41.0] - 2026-05-08 — codd init lexicon auto-suggest (cmd_439)

### Added

- `codd init --suggest-lexicons/--no-suggest-lexicons` now detects common project
  manifests and offers lexicon plug-in suggestions.
- Added generic `codd.init.StackDetector` plus `codd_plugins/stack_map.yaml`; stack
  and lexicon knowledge stays in plug-in data, not core code.
- Suggested lexicons are appended to `project_lexicon.yaml` after user confirmation.

### Quality Metrics

- **pytest**: 2615 PASS / 0 FAIL / 0 SKIP
- **Generality Gate**: `codd/init/` + `codd/cli.py` stack product literal zero hit

## [1.40.0] - 2026-05-08 — Lexicon 30本 完全整備 マイルストーン (cmd_438)

### Added — 31 lexicons (cmd_438 batch1〜5 完走)

cmd_438 lexicon 30本 Sub-Agent 並列整備が完走。`codd elicit --lexicon <id>` で
公式 spec 由来の coverage-check が **7 領域 30 lexicons / ~280+ axes** で動作する。
BABOK 含めて計 31 lexicons を `codd_plugins/lexicons/` に同梱。

#### 領域別 lexicon 一覧

| 領域 | Lexicons (axis 数概算) |
|------|------------------------|
| Methodology base | babok (13) |
| Web | web_responsive (8) / web_a11y_wcag22_aa (13) / web_security_owasp (14) /
        web_performance_core_web_vitals (6) / web_authn_webauthn (6) /
        web_forms_html5 (8) / web_browser_compat (5) / web_seo_schemaorg (8) /
        web_pwa_manifest (7) |
| Mobile | mobile_ios_hig (12) / mobile_android_material3 (12) / mobile_a11y_native (8) |
| Backend / API | api_rest_openapi (15) / backend_grpc_proto (8) /
                  backend_graphql (10) / backend_event_cloudevents (6) |
| Data | data_relational_iso_sql (10) / data_nosql_jsonschema (10) /
         data_eventsourcing_es_cqrs (6) |
| Ops / Cloud | ops_observability_otel (8) / ops_kubernetes (12) /
                ops_iac_terraform (10) / ops_cicd_pipeline (7) |
| Compliance / Governance | data_governance_appi_gdpr (12) / ai_governance_eu_act (10) /
                            compliance_hipaa (10) / compliance_pci_dss_4 (12) /
                            compliance_iso27001 (14) |
| Methodology / Test | process_iso25010 (8) / process_test_iso29119 (6) |

#### Lexicon パッケージ標準構造

各 lexicon は `codd_plugins/lexicons/<id>/` 配下に共通レイアウトで提供:

- `manifest.yaml` — name / version / source_url / source_version / observation_dimensions / references
- `lexicon.yaml` — coverage_axes (axis_type / variants / criticality / rationale)
- `severity_rules.yaml` — axis × coverage 状態 → severity (critical/high/medium/info) 振り分け
- `coverage_matrix.md` — axis のカバー条件説明 (人間レビュー用)
- `elicit_extend.md` — `extends:` で `codd/elicit/templates/elicit_prompt_L0.md` を継承し、
  axis 別 covered/implicit/gap 判定例を記述
- `recommended_kinds.yaml` — 推奨 finding kind (open list、core hardcode 禁止)

### batch 完走履歴 (cmd_438)

| Batch | 領域 | Lexicons | Commits |
|-------|------|----------|---------|
| batch1 | Web 基盤 | web_responsive / web_a11y_wcag22_aa / web_security_owasp | `ffb4aa0` / `210db5d` |
| batch2 | Compliance + Mobile + API | APPI/GDPR / EU AI Act / iOS HIG / Material 3 / OpenAPI | `20fb2fd` / `032cd0e` / `94cf190` |
| batch3 | Compliance + Data + Ops | HIPAA / PCI DSS / ISO SQL / JSON Schema / OTel | `ff13def` / `4189f2a` / `fd1a675` |
| batch4 | Web 拡張 + Backend + Ops | Core Web Vitals / WebAuthn / proto3 / GraphQL / Kubernetes / Terraform | `3938524` / `608e18a` / `37a0b6e` |
| batch5 | Web 残 + Misc + Process | HTML5 forms / Baseline / Schema.org / PWA / Mobile A11y / CloudEvents / EventSourcing / GitOps / ISO 27001 / 25010 / 29119 | `5108ed7` / `73b97a0` / `a0864f2` / `e9aed62` |

### Quality Metrics

- **pytest**: 2600 PASS / 0 FAIL / 0 SKIP (v1.39.0 2363 → +237)
- **新 node/edge/check/SDK 依存**: 全 0
- **Generality Gate 三層 (Layer A core / Layer B template / Layer C lexicon)**: zero hit
  - core code に lexicon 名 hardcode 0
  - template に specific lexicon 名 hardcode 0
  - lexicon plug-in 配下のみ project 固有名 OK
- **backward compatible**: 既存 elicit / diff / brownfield API 互換維持

### Generality Gate 維持の証

`cmd_438` は **15 commits / 31 lexicons / ~280+ axes** を追加したが、CoDD core
(`codd/elicit/*.py` / `codd/diff/*.py` / `codd/brownfield/*.py`) には specific
lexicon 名 / stack 名 / framework 名 / vendor 名の hardcode を **1 件も入れずに**
完走した。axis 値はすべて公式 spec literal (W3C / OWASP / OpenAPI / ISO / HHS /
PCI SSC / OpenTelemetry / Schema.org / Apple HIG / Material 3 / GDPR / EU AI Act)
を出典として採用。

### 殿哲学への接続

「機能要件 + 制約だけ書けば全自動」北極星に対し、この 31 lexicons は **「制約」を
公式 spec に置いた lookup table** として機能する。`codd elicit --lexicon
<id>` で domain ごとの coverage-check が即座に走り、要件に潜む observation
dimension の gap を発見する。

## [1.39.0] - 2026-05-08 — RepairLoop strategy v2 (cmd_432) + batch1 lexicons (cmd_438)

### Added — RepairLoop strategy v2 (cmd_432, commit 54d3ce5)

LLM patch dry-run validation 失敗を「即 abort」から「error-injected retry +
unrepairable 分類」に切替えた retry strategy 実装 (推奨案 D)。

- `codd/repair/llm_repair_engine.py` 拡張: validate failure 時に error 文字列を
  generic に LLM へ再注入、新 patch を提案させる retry path を追加 (~132 insertions)
- `codd/repair/templates/repair_strategy_meta.md` 新規 (~44 LOC、prompt template)
- `codd/repair/git_patcher.py` 微調整 (~27 insertions)
- 失敗時は honest classification (unrepairable に分類) で transparency 確保

osato-lms PoC 結果: unrepairable は 2→8 件と「増加」したが、これは silent
failure を honest unrepairable に置換した分類精度向上 (transparent 化)。
追加の root cause (auth_design.md proof break placeholder 不在) は cmd_432_poc
で identified、別 cmd で対処予定。

### Added — Lexicon batch1 (cmd_438 pilot 3 lexicons)

`codd_plugins/lexicons/` 配下に Web 領域 3 lexicons 追加:

- `web_responsive` (commit ffb4aa0、MDN media queries、8 axes)
- `web_a11y_wcag22_aa` (commit ffb4aa0、WCAG 2.2 W3C Recommendation、13 axes)
- `web_security_owasp` (commit 210db5d、OWASP Top 10 2021 + ASVS 4.0、14 axes)

各 lexicon に `manifest.yaml` / `lexicon.yaml` / `severity_rules.yaml` /
`coverage_matrix.md` / `elicit_extend.md` / `recommended_kinds.yaml` の標準
パッケージ構造を採用。`codd elicit --lexicon <id>` で coverage-check mode が
即動作する。

### Quality Metrics

- **pytest**: 2363 PASS / 0 FAIL / 0 SKIP (v1.38.0 2337 → +26)
- **新 node/edge/check/SDK 依存**: 全 0
- **Generality Gate**: 三層 (Layer A core / Layer B template / Layer C lexicon)
  zero hit
  - core code に lexicon 名 (web_responsive / wcag / owasp / babok) hardcode 0
  - template に specific lexicon 名 hardcode 0
  - lexicon plug-in 配下のみ project 固有名 OK
- **backward compatible**: legacy auto-repair path (validate fail → REPAIR_FAILED)
  も `RepairStrategy.legacy` で利用可能

### Phase 構成と commits

- cmd_432 retry strategy (`54d3ce5`、v1.38.0 統合済) — repair strategy v2
- cmd_438 pilot batch1 (`ffb4aa0` + `210db5d`) — Web 領域 3 lexicons
- release (本 release commit) — v1.39.0 release commit

## [1.38.0] - 2026-05-08 — Brownfield Pipeline (cmd_437)

### Added — `codd brownfield` パイプライン (cmd_437_phase_b)

`codd extract` → `codd diff` → `codd elicit` を統合した brownfield 解析
パイプラインを `codd/brownfield/` 配下に新設。既存実装からの drift 検知 +
spec discovery を 1 コマンドで通せるようになった。

#### 主要モジュール

- `codd/brownfield/__init__.py` — public API
- `codd/brownfield/pipeline.py` — extract → diff → elicit を逐次実行する
  generic オーケストレーター (~307 LOC)
- `tests/brownfield/test_pipeline.py` — 統合テスト (~359 LOC)

#### 設計方針

- **Generic dispatch**: パイプライン段階は名前 (`extract` / `diff` / `elicit`)
  で識別、stack/framework/domain literal hardcode なし
- **既存 module 再利用**: `codd.extract_ai` / `codd.diff` / `codd.elicit` を
  ライブラリ呼び出し、orchestration ロジックのみ新設
- **Finding 抽象共有**: diff / elicit は `codd.elicit.finding.Finding`
  dataclass を共有 (cmd_431 で確立した抽象を活用)

### Quality Metrics

- **pytest**: 2337 PASS / 0 FAIL / 0 SKIP (v1.37.0 2320 → +17)
- **新 node/edge/check/SDK 依存**: 全 0
- **Generality Gate**: Layer A zero hit (`codd/brownfield/*.py` に
  django/flask/fastapi/pydantic/sqlalchemy/next.js/react 等 0 件)
- **backward compatible**: 既存 `codd extract` / `codd diff` / `codd elicit` は
  独立実行可能、本パイプラインは合成のみ

### Phase 構成と commits

- cmd_437_phase_b (`78647c2`) — `codd brownfield` パイプライン本体
- release (`8fae292` v1.37.0 → 本 release) — v1.38.0 release commit

## [1.37.0] - 2026-05-07 — codd diff Bug Fix (cmd_436_phase_d)

### Fixed — codd diff PoC blocker 2 件解消

cmd_436 brownfield drift 検知 PoC で発見された 2 bugs を修正、`codd diff` を実用
レベルへ。

#### Bug 1: severity Literal バリデーション abort

LLM が `Finding.severity` に Literal 外の値 (`low` / `warning` / `urgent` /
`blocker` 等) を返すと `Finding.from_dict` が `ValueError` で停止し、pipeline 全体が
abort する不具合を解消。

- `_coerce_severity` helper を `codd/elicit/finding.py` に追加
- alias 辞書で `blocker/fatal/severe/urgent → critical`、`error/major/important
  → high`、`warn/warning/moderate → medium`、`minor/low/informational/note/trivial
  → info` に正規化
- 不明値は `info` に fallback (LLM drift 耐性)
- canonical 4 値 (`critical/high/medium/info`) はそのまま採用

Generality Gate: alias 辞書は generic な severity synonym のみ、stack /
framework / domain literal なし。

#### Bug 2: codd diff default extract-input パス問題

cmd_437 (v1.36.0) Issue #17 で `codd extract` の output を `.codd/extract/`
配下に隔離した一方、`codd diff` のデフォルト extract-input は legacy の
`codd/extracted.md` を期待し続けていたため、`No such file or directory` で abort。

- `_resolve_diff_extract_input(project_root, value)` helper を追加
- 解決優先順:
  1. 明示 `--extract-input` 指定
  2. `.codd/extract/extracted.md` (Issue #17 isolation target)
  3. `<project_root>/extracted.md` (top-level aggregated output)
  4. `.codd/extract/modules/*.md` の最初のファイル (deterministic by name)
  5. legacy `codd/extracted.md` (互換性維持)
- `codd diff apply` も同 helper を経由して整合

### Quality Metrics

- **pytest**: 2320 PASS / 0 FAIL / 0 SKIP (v1.36.0 と同水準を維持)
- **Generality Gate**: severity alias 辞書は generic synonym のみ、stack /
  framework / domain literal hardcode 0
- **既存 test の追従**: 旧 `test_finding_from_dict_rejects_invalid_severity`
  を `test_finding_from_dict_coerces_severity_aliases` に変更、coerce 動作を
  正本テストとして昇格

### Phase 構成と commits

- cmd_436_phase_d_poc (`origin/main` 経由) — ashigaru1 PoC report で 2 bugs 検出
- cmd_436_phase_d fix (`e969f58`) — 軍師直接実装の bug fix bundle

## [1.36.0] - 2026-05-07 — Brownfield Pipeline Pre-fix (cmd_437)

### Issue Fixes (cmd_437_pre_fix bundle)

`cmd_437` brownfield pipeline 着手前の必須 Issue 3 件を解消し、`codd extract` の
基盤を整備した patch release。

#### Issue #17: extract output 隔離 (cmd_437_pre_fix_a)

`codd extract` が target dir を汚染する不具合を解消。`--output` の default を
`<config-dir>/extracted/` に正規化し、target tree への直接書き込みを禁止。
明示的に target 配下を指定された場合のみ許容。

#### Issue #18: Brownfield --init frontmatter (cmd_437_pre_fix_a)

`codd extract --init` 実行時、出力 YAML/MD に `codd:` frontmatter を自動付与。

```yaml
codd:
  version: "1.0"
  extracted_at: "<ISO8601>"
  source: "<target_path>"
```

後続の `codd diff` / `codd elicit` apply フローで brownfield 由来であることを
判定可能になる。

#### Issue #19: extract Python AST 対応 (cmd_437_pre_fix_b)

`codd extract` を Python (`.py`) ファイルに対応。標準 `ast` モジュール経由の
generic な抽出 (関数定義 / class 定義 / import / module-level docstring) +
構文エラー時の raw text fallback。`tests/extract/fixtures/sample_python/` を
同梱して回帰防止。

### Generality Gate Fix (cmd_437_pre_fix_qc)

cmd_437_pre_fix_b の初期実装が `codd/dependency_catalog.py` に Python 専用の
framework/ORM/test framework 辞書 (django/fastapi/flask/sqlalchemy/prisma/
pytest 等) を hardcode していた問題を release blocker として解消。

- `codd/dependency_catalog.py` 削除
- `codd/extractor.py:_detect_python_patterns` を documented no-op に変換、
  framework/ORM/test 検出は `codd/extract_ai.py` (LLM 動的判断) に委任
- `tests/test_extract.py:test_framework_detection` を no-op default に追従

Generality Gate Layer A (CoDD core に stack/framework 名 hardcode 禁止) 維持。
既存 JS / Prisma 系 hardcode (cmd_437 以前から存在) は別 cmd で順次対処予定。

### Quality Metrics

- **pytest**: 2320 PASS / 0 FAIL / 0 SKIP (v1.35.0 2285 → +35)
- **新 node/edge/check/SDK 依存**: 全 0
- **Generality Gate Layer A**: `dependency_catalog.py` 削除で CoDD core の
  Python framework hardcode 0 件
- **新規 test fixture**: `tests/extract/fixtures/sample_python/` (Python AST 動作確認)
- **backward compatible**: extract API は維持、`detected_frameworks` 等は
  AI extraction 経由で同等情報を取得

### Phase 構成と commits

- cmd_432 retry rebase (`54d3ce5`) — RepairLoop strategy retry follow-up
- cmd_437_pre_fix_a (origin `2ff2ee2`) — Issue #17/#18 (extract output isolation +
  --init frontmatter) を含む統合 commit (ashigaru3 経由で push)
- cmd_437_pre_fix_b core — Python AST 抽出本体 (上記同 commit に同梱)
- cmd_437_pre_fix_qc (`b242b67`) — Generality Gate fix (dependency_catalog 削除)

## [1.35.0] - 2026-05-07 — codd elicit (Coverage/Spec Discovery Engine)

### Added — `codd elicit` 北極星直結機能 (cmd_431)

CoDD の北極星「機能要件 + 制約だけ書けば全自動」への最接近機能 release。
要件・設計に潜む観点漏れ / axis 候補 / 仕様の穴を LLM 動的判断で発見し、
人間レビュー → project_lexicon.yaml / requirements.md / pending への書き戻しまで
一気通貫の **Coverage/Spec Discovery Engine** を導入。

#### 主要機能

- **`codd elicit`** — greenfield discovery (lexicon 不在時 ~10 件 high-signal findings)
- **`codd elicit --lexicon <path>`** — lexicon-loaded coverage-check mode (gap only)
- **`codd elicit apply <input>`** — approved findings を `project_lexicon.yaml` /
  `requirements.md` / `.codd/elicit/ignored_findings.yaml` に書き戻し
- **multi-formatter** — `--format md` (default、`findings.md`) / `json` (CI) /
  `--interactive` (CLI REPL Y/n/d)
- **永続化** — `.codd/elicit/{ignored,pending,history}.yaml` で重複質問回避

#### Coverage-mode (cmd_431_coverage_mode_impl)

lexicon ロード時に「discover」モードから「coverage-check」モードへ自動切替:

- lexicon-defined categories を `covered` / `implicit` / `gap` に分類
- `findings` は `gap` のみ emit (noise 削減)
- `lexicon_coverage_report` で全カテゴリの分類結果を可視化
- `all_covered: true` で全カテゴリ充足を明示
- legacy array payload (discover mode) は backwards compatible

#### BABOK lexicon plug-in 同梱

`codd_plugins/lexicons/babok/`:
- BABOK 13 dimensions (stakeholder / goal / flow / issue / data / functional /
  non-functional / rule / constraint / acceptance / risk / assumption / term)
- coverage-check classification rules (`elicit_extend.md`)
- recommended kinds (open list、core hardcode 禁止)

#### 出力スキーマ

`ElicitResult` dataclass:

```python
{
  "all_covered": false,
  "lexicon_coverage_report": {
    "stakeholder": "covered",
    "goal": "gap",
    ...
  },
  "findings": [
    {"id": "...", "kind": "<LLM 動的>", "severity": "critical|high|medium|info", ...}
  ],
  "metadata": {}
}
```

`kind` は **LLM 動的判断**で生成 (core hardcode 禁止、Generality Gate 維持)。
`severity` enum 固定 (`critical` / `high` / `medium` / `info`)。

### osato-lms PoC 実証

lexicon=BABOK で本日実証:

```
13 categories classified:
  covered:  4 (functional / issue / non-functional / stakeholder)
  implicit: 3 (assumption / flow / term)
  gap:      6 (acceptance / constraint / data / goal / risk / rule)

findings: 12 (legacy discover mode) → 6 (gap only、50% noise reduction)
```

### Quality Metrics

- **pytest**: 2285 PASS / 0 FAIL / 0 SKIP / 12 warnings (v1.34.0 2145 → +140)
- **新 node/edge/check/SDK 依存**: 全 0
- **Generality Gate**: Layer A (code) + Layer B (template) zero hit
  - kind 列挙 hardcode 禁止 (`axis_candidate` / `spec_hole` / `babok` 等は core 不知)
  - stack/framework/domain 名 hardcode 禁止
  - lexicon 名は plug-in 配下のみ、core code 0 件
- **backward compatible**: legacy `list[Finding]` payload は formatters / from_payload で吸収

### Phase 構成と commits

- cmd_431_phase_b core (`3fc2b35`) — Finding dataclass + ElicitEngine + persistence
- cmd_431_phase_c lexicon (`29bf6fe`) — Lexicon loader + BABOK plug-in
- cmd_431_phase_d formatters/apply (`67256a3`) — md/json/interactive + ElicitApplyEngine
- cmd_431 coverage-mode (`8d3f332`) — ElicitResult + coverage-check + 13 dimensions classification

## [Roadmap] — 北極星接続フェーズ (v1.36.0+)

### Roadmap (確定済 design session 2026-05-07)

殿の北極星「機能要件 + 制約だけ書けば全自動」への最大ギャップ = **要件完全性の前提**。
`codd elicit` で要件側の穴を AI が発見し、Yes/No 承認で要件強化する仕組みを v1.35.0 から段階導入する。

| Release | 機能 | 状態 |
|---------|------|------|
| **v1.35.0** | **`codd elicit`** — 要件→axis候補+spec穴の Discovery Engine | cmd_431 軍師発散中 |
| v1.36.0 | `@codd/lexicon/babok` 同梱 + multi-formatter (md/json/PR comment) | cmd_434 起票予定 |
| v1.37.0 | **`codd diff`** — brownfield drift 検知 (要件 vs 実装) | cmd_435 起票予定 |
| v1.38.0 | extract → diff → elicit パイプライン化、brownfield 完全フロー | cmd_436 起票予定 |
| v1.39.0 | unrepairable 削減 (RepairLoop strategy 汎用化) | cmd_432 軍師発散中 |
| v1.40.0 | 他ドメイン dogfooding (Mobile/CLI/embedded etc) | 案件選定後 |
| (v2.0.0) | elicit ↔ verify 双方向 loop (北極星) | future |

#### `codd elicit` 設計確定事項 (v1.35.0)

design session で確定した仕様 (詳細は [README.md](README.md) の North Star 接続セクション参照):

- **Q1 コマンド名**: `codd elicit` (要件工学 requirements elicitation 由来)
- **Q2 prompt template**: L0 + L2 + L3 (BABOK lexicon optional plug-in、`extends:` 方式)
- **Q3 brownfield**: 案β (extract→elicit パイプ、`codd diff` 新コマンドで連動)
- **Q4 出力構造**: 統合 `findings: []` + LLM動的 kind + severity enum固定 (critical/high/medium/info)
- **Q5 承認 UI**: 多モード対応
  - default `findings.md` (殿Zenn流儀、git管理)
  - `--interactive` CLI inline (REPL Y/n)
  - `--format json` (CI連携)
  - `--post-pr <N>` GitHub PR comment (plugin)
  - `codd elicit apply <input>` で書き戻し (project_lexicon.yaml + requirements.md + ignored_findings.yaml)
- **Q6 verify 統合**: 緩い (verify report に「elicit 推奨」suggestion のみ、自動起動なし)
- **Q7 ループ性**: ignored_findings.yaml + pending_findings.yaml + elicit_history.yaml で差分質問

#### Generality Gate (v1.35.0+ 厳守)

- `kind` (axis_candidate / spec_hole / regulation_hint 等) を core hardcode 禁止 (LLM動的、lexicon推奨のみ)
- `severity` のみ enum 固定 (consumer 側 deploy gate 判定の generic 概念)
- BABOK lexicon は **optional plug-in**、ロードしなければ動作変化なし

### Recent Patches (since v1.34.0 release)

#### 2026-05-06 — cmd_427 README v1.34.0 リライト (3か国語)
- README_ja.md / README.md / README_zh.md を v1.34.0 に対応
- Quick Start / Use Cases / 実証ケーススタディ / 4-release 進化を全面更新

#### 2026-05-06 — cmd_428 case study 匿名化 (`df6b450`)
- README 内の固有プロジェクト名を「実プロジェクト LMS Web App」表記に変更
- スタック (Next.js + Prisma + PostgreSQL) は明示維持、識別可能情報を削除

#### 2026-05-07 — cmd_429 CI audit workflow fix (`531a66f`)
- `audit.yml` workflow が `FileNotFoundError: /tmp/codd-audit.json` で exit 1 する bug 修正
- README-only PR (design書無変更) でも audit が gracefully PASS する挙動に
- workflow 自体に jq parse fallback 追加 → audit json 生成失敗時も CI green

#### 2026-05-07 — README v1.35.0 ロードマップ追記 (3か国語)
- 「北極星」「現在地 (v1.34.0)」「Roadmap v1.35.0-v2.0.0」「North Star 接続: codd elicit」セクション追加
- 「実用到達点 100% (真)」表現を「Web Next.js Prisma+TS 単一viewport で auto-repair PARTIAL_SUCCESS 完走」に正直化
- 境界条件と未解決ギャップを明示

---

## [1.34.0] - 2026-05-06 — Full pipeline auto-repair 実プロジェクト完走

### Achievement — 「自律自己修復実装駆動」の最終形 (cmd_425 7 commits bundle)

CoDD は **v1.31.0「内側 100%」→ v1.32.0「外側 100%」→ v1.33.0「caveats 解消経路実証」→
v1.34.0「full pipeline 完全実証」** の 4 release で「実用到達点 100% (真)」に到達した。

osato-lms 実プロジェクト上で `codd verify --auto-repair --max-attempts 10` が
**PARTIAL_SUCCESS で完走** (attempts=4 / applied_patches=4 / pre_existing=1 / unrepairable=2)、
v1.33.0 caveat_1「full pipeline 完走できない」を構造解消。
殿哲学「機能要件 + 制約だけ書けば全自動」が **実プロジェクト dogfooding で実証**された記念 release。

### cmd_425 — 7 phase bundle

#### cmd_425_a — RepairLoop multi-violation sequential improvement (`c1782ec`)

RepairLoop の loop continuation logic 改善:

- `max_attempts=10` (default、ASK-1=B 反映)
- `baseline_ref` 引数追加 (default = 起動直前 HEAD、ASK-6=B 反映)
- `RepairResult.status` enum 拡張: SUCCESS / PARTIAL_SUCCESS / REPAIR_FAILED / MAX_ATTEMPTS_REACHED (ASK-5=B)
- 中間 `applied_patches` 保持機構

#### cmd_425_b — Repairability Classifier (Hybrid: git diff + LLM) (`e02f5bc`)

ASK-3=C 反映の Hybrid 実装:

- **Stage 1 (git diff heuristic)**: `git diff baseline_ref..HEAD` で violation の affected_files
  が変更されたか機械判定 → 変更あり = repairable (in-task)、なし = stage 2 へ
- **Stage 2 (LLM 判断)**: 不明瞭 violation を LLM に判断させる (in-task / pre_existing /
  unrepairable の 3 分類)
- `NullClassifier` fallback 提供 (テスト/先行実装期間用)
- `repairability_meta.md` prompt template (Generality 維持、specific check name hardcode 禁止)

#### cmd_425_c — Primary Violation Picker (DAG order) (`5e93495`)

複数 repairable violation から最 upstream を選択:

- DAG topological order で sort
- 同 level なら severity (critical > high > medium > info)
- それも同点なら timestamp 順 (古いものを優先)
- `FirstViolationPicker` fallback 提供

#### cmd_425_a2 — Hybrid Classifier 統合 fix (`32d4ffb`)

cmd_425_a + cmd_425_b の統合:

- `_default_repairability_classifier(config)` で config 経由 RepairabilityClassifier 注入
- CLI `cli.py` で `llm_client=SubprocessAiCommand(...)` + `repo_path=project_root` を
  `RepairLoopConfig` に設定
- 統合 unit test 追加 (CLI 起動時の Hybrid 動作確認)

#### cmd_425_a3 — propose_fix exception handling fix (`4db478b`)

`engine.propose_fix` 例外時の handling 改善:

- 旧実装: 例外 → 即 `return REPAIR_FAILED` で `attempts.append` なし → 1 violation の修復不能
  で全 cmd_425_lms が落ちる
- 新実装: 例外 → 当該 violation を `unrepairable` に分類 + `continue` → 次 attempt で別
  violation を pick

#### cmd_425_a4 — Status 判定意味論統一 (`cc6da48`)

ASK-2 (skip + report) + ASK-5 (PARTIAL_SUCCESS) の意味論を統一:

- 新ヘルパー `_classified_work_status()` 追加
- `applied_patch_files OR pre_existing OR unrepairable → PARTIAL_SUCCESS`、
  else → `REPAIR_FAILED`
- 3 箇所の status 判定を統一 (line 175 全 unrepairable / line 198 propose_fix exception /
  line 299 apply exception)

#### cmd_425_lms — osato-lms full pipeline 完走実証 (osato-lms `3262f9b`)

`codd verify --auto-repair --max-attempts 10` 実行結果:

- **status=PARTIAL_SUCCESS** (前 v1.33.0 で REPAIR_FAILED から改善)
- attempts=4 / applied_patches=4 / pre_existing=1 / unrepairable=2
- 修復された files: `tests/e2e/environment-coverage.spec.ts` / `tests/e2e/login.spec.ts`
- 残 violation: deployment_completeness chain (pre_existing) / Dockerfile dry-run (unrepairable)
  / Vitest matcher runtime (unrepairable)
- smoke proof 6 checks PASS
- **CoDD core 改修 0 行** (Generality 完全維持)

### Quality Metrics

- **pytest**: 2145 PASS / 0 FAIL / 0 SKIP / 12 warnings (v1.33.0 2068 → +77)
- **新 node_kind / edge_kind / drift_event / SDK 依存**: 全 0
- **新 check_kind**: 0 (既存 RepairLoop の loop logic + status 判定改善のみ)
- **Generality Gate**: 二層 (code A / template hint B) zero hit
- **backward compatible**: 既存 v1.33.0 RepairLoop API は API 互換維持

### Fix Cycle Note (透明 disclosure)

cmd_425_a の初期実装 (`c1782ec`) は単独では実プロジェクトで完走できず、3 段の fix
(`a2` 統合 → `a3` exception handling → `a4` status 判定) を経て収束。これは **実プロジェクト
dogfooding でしか発見できない integration 問題の収束過程**として価値あり。

CoDD 自身が CoDD を改善する self-improving 性質の証。各 fix サイクルは:

1. cmd_425_a unit test PASS だが CLI 起動時に Hybrid 動かず (NullClassifier fall back)
2. cmd_425_a2 で CLI 統合した後、propose_fix 失敗で attempts=0 即終了
3. cmd_425_a3 で exception handling 改善した後、全 unrepairable 時 REPAIR_FAILED 返却
4. cmd_425_a4 で「skip + report 完了 = PARTIAL_SUCCESS」意味論統一して収束

### Caveats (info、release blocker なし)

- **osato-lms 残 violation 3 件**: pre_existing 1 (deployment_completeness chain) +
  unrepairable 2 (Dockerfile dry-run / Vitest matcher runtime)。これらは ASK-2/ASK-5 反映の
  正常挙動 (CoDD 責任外として skip + report)。osato-lms baseline の他 check 項目の問題で、
  v1.35.0 候補 cmd_426 (osato-lms baseline cleanup) で解消予定。

### Phase 構成と commits

- cmd_425_a (`c1782ec`) — RepairLoop multi-violation sequential
- cmd_425_b (`e02f5bc`) — Repairability classifier (Hybrid)
- cmd_425_c (`5e93495`) — Primary picker (DAG order)
- cmd_425_a2 (`32d4ffb`) — Classifier 統合 fix
- cmd_425_a3 (`4db478b`) — propose_fix exception handling fix
- cmd_425_a4 (`cc6da48`) — Status 判定意味論統一
- cmd_425_lms (osato-lms `3262f9b`) — full pipeline PARTIAL_SUCCESS 完走実証

### Milestone — 「実用到達点 100% (真)」

CoDD の 4 release 進化:

| Release | 到達点 |
|---------|--------|
| v1.31.0 | 内側 100% (内部整合性 coherence) |
| v1.32.0 | 外側 100% (対象環境網羅性 Coverage Axis) |
| v1.33.0 | caveats 解消経路実証 (実機 CDP / typecheck loop) |
| **v1.34.0** | **full pipeline 完全実証 (auto-repair PARTIAL_SUCCESS 完走)** |

殿哲学「機能要件 + 制約だけ書けば全自動」が実プロジェクト osato-lms で実証された
**記念マイルストーン release**。

## [1.33.0] - 2026-05-06

### Resolved — v1.32.0 Caveats Resolution (cmd_423 + cmd_424)

v1.32.0 で transparent disclosure した 3 caveats のうち **2 件を完全解消、1 件を構造実証**。
osato-lms 統合実証が end-to-end レベルへ進み、CoDD は **内側 (内部整合性) + 外側 (対象環境網羅性)
の両次元 coherence** を実機で確認。

### cmd_424 — Stripe Webhook Cleanup + cmd_420 Typecheck Loop 実証 (caveat_3 完全解消)

osato-lms 既存 dirty Stripe webhook route の missing module imports (cmd_412b 期間中の
取りこぼし) を方針 B (route 簡素化) で解消、cmd_420 typecheck loop の実プロジェクト動作を
実証。

- **osato-lms 28c4144**: Stripe webhook route 簡素化 (`@/lib/api/rate-limiter` /
  `@/lib/stripe/client` / `@/modules/payments/handlers` / `@/modules/payments/types`
  欠落 import を削除し Stripe SDK 直使用)
- `npm run typecheck` PASS
- `codd implement run --enable-typecheck-loop` PASS — cmd_420 typecheck loop が osato-lms で
  実機動作確認

### cmd_423 — osato-lms LMS Integration (caveat_2 完全解消 + caveat_1 構造実証)

osato-lms に cdp_browser config + project CDP plugin を統合し、smartphone_se 実機 journey
PASS と C9 auto-repair 経路の attempt_0 patch apply を実証。

- **osato-lms 3314ae5**:
  - `codd/codd.yaml` に `verification.templates.cdp_browser` 設定 (engine=edge, port=9222,
    launcher=shell_script)
  - `codd_plugins/` (cdp_browser launcher / engine plugin)
  - `scripts/start-cdp-browser.sh` (Edge launcher)
  - `docs/design/auth_design.md` login_to_dashboard journey 実機 CDP 化

- **codd-dev 6ecdeaf**:
  - project CDP plugin loader 対応
  - RepairLoop file path fallback
  - standalone verify の C9 inclusion

#### caveat_2 完全解消: 実機 CDP run-journey PASS

```
codd dag run-journey login_to_dashboard --path /home/tono/osato-lms \
                                         --axis viewport=smartphone_se
→ PASS (executed 3 CDP journey step(s))
```

スマホ事故再発防止の構造的閉ループが完成。viewport=smartphone_se で central_admin の
login → dashboard 到達を **実機 Edge CDP** で確認。

#### caveat_1 構造実証: C9 auto-repair 経路 attempt_0 PASS

意図的 violation 仕込み (`project_lexicon.yaml` viewport variant id を
`smartphone_se_missing` に変更) → C9 red 2 件発生 → `codd verify --auto-repair`
attempt_0 で:

- RCA: `affected_nodes=[project_lexicon.yaml], strategy=full_file_replacement`
- proposal: `smartphone_se_missing` を `smartphone_se` に戻す full_file_replacement
- apply: `success=true, applied_patches=[project_lexicon.yaml]`
- post-check: **C9 environment_coverage PASS**

LLM patch 生成 → apply → post-check PASS の閉ループを **実プロジェクトで実証**。

### Quality Metrics

- **codd-dev pytest** (関連抜粋): repair/verify_runner / cdp_browser_axes / cdp_browser_core
  / cli_run_journey / verify_auto_repair_standalone — **95 PASS / 0 FAIL / 0 SKIP**
- **osato-lms vitest**: tests/e2e/environment-coverage.spec.ts **3 PASS**
- **osato-lms prisma:validate**: PASS
- **git diff --check**: codd-dev / osato-lms 両者 clean
- **新 node/edge/enum/drift/SDK 依存**: 全 0 (cmd_422 attribute 拡張パターン継続)
- **Generality Gate**: zero hit

### Caveats (透明 disclosure)

cmd_423 で C9 auto-repair 経路は実証されたが、**full `codd verify --auto-repair` pipeline
は最終 `REPAIR_FAILED` 判定で完走できず**:

- C9 修復 (attempt_0 PASS) の後、osato-lms 既存 baseline の `node_completeness` /
  `deployment_completeness` 失敗および Vitest matcher / no-tests 系 failure が残存し、
  次 attempt は別 proof-break (auth proof-break) に落ちた
- これは **C9 修復経路自体の問題ではなく**、osato-lms baseline の他 check 項目の問題

**v1.34.0 候補 (cmd_425 等)**: osato-lms baseline 修復 (node_completeness /
deployment_completeness / Vitest matcher 整備) で full auto-repair pipeline 完走を達成予定。
完成すれば「自律自己修復実装駆動」の最終形に到達する。

### Phase 構成と commits

- cmd_424 (osato-lms `28c4144`) — Stripe webhook typecheck debt fix
- cmd_423 lms (osato-lms `3314ae5`) — CDP browser proof integration
- cmd_423 codd (codd-dev `6ecdeaf`) — Support project CDP plugins in repair proof

## [1.32.0] - 2026-05-06

### Added — Coverage Axis Layer (cmd_422 6 phase bundle)

v1.31.0「実用到達点 100%」で発覚した「内側のみ完成、外側 (対象環境網羅性) 未対応」を構造解消。
要件で宣言された **axis × variant の網羅** を統一抽象 (CoverageAxis + CoverageVariant) で
吸収し、viewport / device / locale / network / a11y / security / time / data_state など
**16+ 軸を統一構造で表現**。stack/framework/domain ごとの個別 detector を作らず、
**Generality Gate を完全維持**したまま「外側次元」を coherence check の対象に編入する。

LMS デモ中央管理者 navbar 消失事故 (smartphone_se viewport で sidebar lg:block 1024px+
かつ getBottomNavigation 空) を起点に設計。「内側 + 外側の両次元 coherence」へ進む release。

### cmd_422_pre — Coverage Axis Scaffold

新 module: `codd/dag/coverage_axes.py`
- `CoverageVariant` dataclass: `id` / `label` / `attributes: dict` / `criticality:
  Literal["critical","high","medium","info"]`
- `CoverageAxis` dataclass: `axis_type: str (open enum)` / `rationale` / `variants` /
  `source: Literal["design_doc","lexicon","llm_derived"]` / `owner_section`
- `extract_coverage_axes_from_lexicon()` — `project_lexicon.yaml` `[coverage_axes]` 抽出
- `extract_coverage_axes_from_design_doc()` — design_doc frontmatter 抽出
- C9 registry skeleton (`codd/dag/checks/environment_coverage.py`)
- `codd/dag/extractor.py` に `coverage_axes` passthrough

### cmd_422_a — C9 environment_coverage Check (block_deploy=True)

新 check (C9) `environment_coverage` を本実装。3 violation type 全検出:

- `missing_test_for_variant` — variant に対応するテスト不在 (severity: per `variant.criticality`)
- `journey_not_executed_under_variant` — journey が variant 配下で未実行
- `variant_criticality_unclear` — criticality 未定義 (amber)

**block_deploy=True 殿 override 反映** (軍師推奨 defer から殿判断で deploy gate 化)。
`codd/dag/builder.py` で `coverage_axes` を DAG に統合、`codd/dag/runner.py` で C9 認識。

### cmd_422_b — cmd_408 CriteriaExpander coverage_axis Source

cmd_408 CriteriaExpander 拡張:
- `CriteriaItem.source` に `"coverage_axis"` を追加 (str open enum 維持)
- `dynamic_items` に `axis × variant` 展開 (variant.criticality を severity に反映)
- `criteria_expand_meta.md` に `coverage_axes_hint` slot 追加

要件由来 axis が CriteriaExpander の入力として自動的に流れる (動的基準展開と統合)。

### cmd_422_c — cmd_410 ImplStep.required_axes + Layer 2 Axis Inference

cmd_410 ImplStep 拡張:
- `ImplStep.required_axes: list[CoverageAxisRef]` 追加 (Layer 1)
- `BestPracticeAugmenter` (Layer 2) で **axis 推論** (HITL gate 必須、double opt-in)
- `best_practice_augment_meta.md` / `impl_step_derive_meta.md` 拡張

implement 段階で「この step は viewport=smartphone_se 配下でも動作する必要があるか」を
LLM が推論し、HITL 承認後に required_axes に反映。

### cmd_422_d — cmd_397 CdpBrowser Axis Runtime Override

cmd_397 verification template に axis 別 runtime 切替を追加:
- `CdpBrowser.execute(axis_overrides: dict[str, str] | None = None)` 拡張
- `codd dag run-journey --axis viewport=smartphone_se` CLI フラグ追加
- `cdp_engines.py` に variant.attributes 適用 (setViewport / setLocale 等の **CDP wire 標準**
  generic dispatch)
- **axis_type 文字列を core code で dispatch しない** (Generality 維持、variant.attributes
  → generic CDP command 変換のみ)

### cmd_422_lms — osato-lms Coverage Axes Proof

osato-lms 実証シナリオ:
- `project_lexicon.yaml` に `coverage_axes` 宣言 (viewport / rbac_role 2 axis)
- `auth_design.md` frontmatter に局所宣言例
- 中央管理者 bottom navigation 修復 (smartphone viewport 用 entries 追加)
- C9 で **16 violations red 検出** → 修復 → C9 pass 構造実証
- `tests/integration/lms_cmd_422_proof.sh` smoke 6 checks PASS
- `tests/e2e/environment-coverage.spec.ts` vitest 3 PASS

### Quality Metrics

- **pytest**: 2068 PASS / 0 FAIL / 0 SKIP / 12 warnings (v1.31.0 1991 → +77)
- **新 node_kind**: 0 / 新 edge_kind: 0 / 新 enum 値: 0 / 新 drift event: 0 / 新 SDK 依存: 0
- **新 check**: 1 (C9 environment_coverage、block_deploy=True)
- **Generality Gate**: 二層 (code A / template hint B) 各 cmd zero hit
- **アーキテクチャ整合性**: 既存 4 node kind (`requirement` / `design` / `implementation` /
  `test`) + 既存 edge kind のみ。axis × variant は **attribute schema 拡張** で吸収

### Caveats (透明 disclosure)

cmd_422 は **codd-dev 側 core 実装は完了**したが、osato-lms との **end-to-end 統合実証** は
3 件の caveat が残存:

1. **cmd_398/420 LLM 自動修復は cmd_422_lms シナリオで未走行** —
   足軽3号が原因箇所 (navigation.ts) を直接修復し、`codd verify --auto-repair` の LLM patch 生成
   path は走行せず。C9 violation は CSS responsive の問題で typecheck RepairLoop の scope 外、
   設計書想定内 (家老判断)。

2. **実機 CDP run-journey 未達成** —
   `codd dag run-journey login_to_dashboard --axis viewport=smartphone_se` は osato-lms
   `codd/codd.yaml` に `verification.templates.cdp_browser` 設定が未定義のため FAIL。
   codd-dev 側 cmd_422_d 実装 (CdpBrowser axis runtime override 48 tests PASS) は完了済。

3. **cmd_420 typecheck loop 動作未確認 (osato-lms 側)** —
   osato-lms の既存 dirty Stripe webhook route が missing module import を持つため
   `npm run typecheck` 自体が失敗、cmd_420 typecheck loop 動作確認まで到達せず。
   codd-dev 側 cmd_420 実装 (v1.31.0 で release) は無回帰。

これらは **codd-dev 側 core 実装は完了済**、osato-lms 側 setup の問題。次 release (v1.33.0
候補 cmd_423 + cmd_424) で osato-lms 側設定追加 + dirty file cleanup 後、auto-repair 統合
実証 + 実機 CDP run-journey + typecheck loop 連動を確認予定。

### Phase 構成と commits

- cmd_422_pre (`f2f6a16`) — Coverage Axis scaffold
- cmd_422_a (`72f6f4c`) — C9 environment_coverage check (block_deploy=True)
- cmd_422_b (`13a33d4`) — cmd_408 CriteriaExpander coverage_axis source
- cmd_422_c (`89bd187`) — cmd_410 axis inference (Layer 2)
- cmd_422_d (`b913091`) — cmd_397 CdpBrowser axis runtime override
- cmd_422_lms (osato-lms `1e8eabf`) — coverage_axes proof + smartphone fix

## [1.31.0] - 2026-05-06

### Added — Practical 100% Achievement (cmd_417 + cmd_418 + cmd_419 + cmd_420 + cmd_421)

cmd_412b QC で発見された 5 件の deviation を **5 cmd bundle で全解消**。
CoDD は v1.30.0 の「理論到達点 100%」から、v1.31.0 で **「実用到達点 100%」記念マイルストーン**
に到達する。「ノールック開発」の閉ループが完全に閉じる release。

特に **cmd_420 (cmd_398 RepairLoop の impl 段階転用)** で「完全無人主張の最大 gap」
(手動 type fix 4 件) を構造的に解消、CoDD は「自己修復実装駆動」の 5 段階目に到達。

### cmd_421 — codd version --check (cmd_412b dev_1)

`codd version --check` で project 要求 vs installed 差分検出 + WARN。
codd.yaml [codd_required_version] declarative 宣言、各 codd subcommand 起動時に互換性
WARN を表示 (--strict で exit 1)。

### cmd_419 — Standalone Auto-Repair Robustness (cmd_412b dev_4)

cmd_404 (v1.30.0) の standalone auto-repair が osato-lms 環境で詰まった問題を解消:

- Prefer standalone verify for auto-repair
- Skip missing proof-break checks with warnings
- `tests/integration/standalone_repair_skeleton/` を同梱 (osato-lms hardcode 回避、
  generic standalone project skeleton で再現可能)

### cmd_420 — Typecheck Repair Loop (cmd_412b dev_5、最大 gap 解消)

CoDD implement 完了後に **自動 typecheck loop** を実行、type 不整合 → cmd_398 RepairLoop で
ループ修復する機構を追加。cmd_412b で発生した「手動 type fix 4 件」(完全無人主張の最大 gap)
を構造的に解消する。

- **新 module**: `codd/implementer/typecheck_loop.py` (TypecheckRepairLoop +
  TypecheckLoopResult dataclass)
- **cmd_398 RepairLoop の impl 段階転用** — RepairEngine ABC + LlmRepairEngine + RepairProposal
  を import 流用、重複実装ゼロ
- **CLI**: `codd implement run --enable-typecheck-loop` (default disabled、明示 opt-in)
- **codd.yaml [typecheck.command]**: project 必須宣言 (TypeScript `tsc` / Rust `cargo check` /
  Go `go build` / Python `mypy` 全対応可、CoDD core にハードコードなし)
- **default**: `max_repair_attempts=3` (cmd_398 と統一)
- **Generality Gate**: TypeScript 固有名 (tsc / npm run typecheck) hardcode ゼロ確認済

### cmd_417 — codd require --check (cmd_412b dev_2)

`codd require --check` で completeness verification を実行。cmd_412b で ashigaru2 が遭遇した
「No such option: --check」エラーを解消。

### cmd_418 — codd implement run task auto-detect (cmd_412b dev_3)

`codd implement run` 単独 (--task 不指定) で project root から task auto-detect。
implementation_plan.md の最新未完了 task or `.codd/derived_tasks/` の最新 approved task を
自動選択、UX 改善。

### Generality Gate (5 cmd 全 zero hit)

- cmd_421/417/418: 軽量、stack/framework/domain 名 hardcode ゼロ
- cmd_419: osato-lms hardcode ゼロ、generic standalone project skeleton で再現
- cmd_420: TypeScript 固有名 hardcode ゼロ、typecheck command は codd.yaml plug-in

### 共通基盤の流用 (重複実装ゼロ)

- cmd_420 が cmd_398 (RepairEngine / LlmRepairEngine / RepairProposal) を全面流用
- cmd_398 で確立した「verify 失敗 → 修復」pattern を「implement 後 typecheck 失敗 → 修復」
  に転用、抽象を再利用

### Note: Python 3.10 互換性 fix (incidental)

`datetime.UTC` (Python 3.11+) → `timezone.utc` (Python 3.10+) で互換性回復。
v1.30.0 release 後の incidental fix。

### 1991 tests PASS / SKIP=0

v1.30.0 baseline 1938 → 現状 1991 (+53 tests = 5 cmd 全 PASS + Python 3.10 互換性 tests)。

### Backwards compatibility

- 5 cmd 関連は完全 opt-in、既存 v1.30.0 ユーザは挙動変化ゼロ
- cmd_420: --enable-typecheck-loop 不指定で legacy path
- cmd_421: codd_required_version 不在で互換性チェック skip
- cmd_417/418: 既存 codd require / codd implement 挙動回帰なし

### 5 件の caveat 全解消

| cmd_412b deviation | v1.31.0 解消 cmd |
|---|---|
| dev_1 (codd CLI バージョン乖離) | cmd_421 |
| dev_2 (codd require --check 未対応) | cmd_417 |
| dev_3 (codd implement run --task 必須) | cmd_418 |
| dev_4 (standalone auto-repair 失敗) | cmd_419 |
| **dev_5 (手動 type fix 4 件、最大 gap)** | **cmd_420** |

### 思想的到達点

CoDD は **「checklist 駆動 → declarative-coverage 駆動 → best-practice-augmented 駆動 →
完全無人自動化駆動 → 自己修復実装駆動」** の 5 段階目に到達。

cmd_393 declarative + cmd_392/393 検証 + cmd_397 実行 + cmd_398 自己修復 + cmd_406-408
Coverage Closure + cmd_410 Implementation 2-Layer + cmd_413-415/404/405 Last-Mile +
**cmd_417-421 Practical 100%** で coherence 系の本来用途が ground truth で実証可能な
レベルに到達した。

「ノールック開発」の閉ループが完全に閉じ、人間の手動補完が構造的に不要になる。

## [1.30.0] - 2026-05-06

### Added — Last-Mile Completion: Chunked Execution + C8 Path Matcher + Layer 2 Visibility (cmd_413 + cmd_414 + cmd_415 + cmd_404 + cmd_405)

cmd_412 ドッグフードで判明した 3 caveats + cmd_398_g1 M_2 + cmd_399 audit item_5 を **5 cmd
bundle で全解消**。CoDD は「ノールック自動化」9 割 → **100% に到達**、完全無人実証が完成する。

3 ashigaru 並列実装が設計見積もり 8.5 h を **15 分で達成** = pattern 確立 (cmd_385/393/397/398/410)
+ 共通基盤 (cmd_397_f/g/h) 流用 + cmd_410-412 経験の三重効果。

### cmd_413 — Chunked Execution + Progress Streaming

`codd implement run` の 900s timeout 解消。ImplStep[] を chunk 分割実行 + 進捗 streaming +
ctrl+C graceful shutdown + USER_INTERRUPTED 後の resume 対応。

- **新 module**: `codd/implementer/chunked_runner.py` (498 LOC、ChunkedRunner +
  ChunkedRunResult / ChunkedExecution dataclass)
- **CLI**: `codd implement run --task <id> --chunk-size <N> --timeout-per-chunk <S>` +
  `codd implement resume --task <id> --history <ISO8601>`
- **Default**: chunk_size=5, timeout_per_chunk=600 (ASK-1 推奨案)
- **history persistence**: `.codd/chunked_run_history/{ISO8601}/chunks/chunk_{N}.yaml` +
  `final_status.yaml` (SUCCESS / PARTIAL / TIMEOUT / USER_INTERRUPTED)
- **graceful kill**: ctrl+C → child process group SIGTERM → graceful shutdown
- **既存 implementer 挙動回帰なし**: --chunk-size 不指定で legacy path

### cmd_414 — C8 Path Matcher 4 段階強化 (caveat_2 公式 merge)

cmd_412 で ashigaru2 が当てた uncommitted patch を **公式 merge**。bracket dynamic route
(`/api/v1/courses/[id]/route.ts`) と src/app prefix の正しい matching を Generality 制約遵守で
正式化、osato-lms で C8 公式 PASS 確認済。

- **`_matches_any_impl` 4 段階**:
  1. exact match (既存)
  2. glob match (既存)
  3. **bracket route normalization** (新): `\[[^\]]+\]` パターンを `*` に展開
  4. **src prefix tolerant matching** (新): `src/`, `lib/`, `app/` の prefix tolerance
- **Generality**: Next.js 固有用語 (page.tsx / route.ts / layout.tsx) を core にハードコード
  禁止、`[...]` 文字列パターンとして抽象化
- **codd.yaml [coherence.path_prefix_tolerant]** で project 上書き可
- **default**: `['src/', 'lib/', 'app/']` (ASK-2 推奨案)

### cmd_415 — Layer 2 BestPracticeAugmenter 貢献ログ可視化 (caveat_3 解消)

`codd implement run` 実行時に Layer 1 (explicit) と Layer 2 (implicit) の各 ImplStep[] を
分離保存 + CLI で内訳表示。

- **cache 拡張**: `.codd/derived_impl_steps/{task_id}.yaml` に Layer 別 fields 維持
- **CLI**: `codd implement steps --task <id> --show-layer-breakdown`
- **出力**: Layer 1 count + Layer 2 count + avg_confidence + 各 step rationale

### cmd_404 — Standalone Auto-Repair CLI (cmd_398_g1 M_2 解消)

`codd verify --auto-repair` が codd-pro 不在環境 (osato-lms 等) で動作するように修正。
verify_runner (Python import 経路、cmd_398_d) を default に組み込み、codd-pro 利用可能時は
従来挙動維持。

### cmd_405 — Frontmatter Attribute Alias (cmd_399 audit item_5 解消)

`codd.yaml [extraction.frontmatter_alias]` で alias mapping を宣言可能に。
`DESIGN_DOC_ATTRIBUTE_KEYS` の lookup を hash map ベースに変更、**全 frontmatter フィールド**
を alias 対応 (ASK-3 推奨案、generic 設計)。

- **例**: Mobile project が `interaction_flows: user_journeys` と alias 宣言、
  Embedded project が `sensor_pipelines: user_journeys` と宣言
- **既存挙動回帰なし**: alias mapping 不在で従来通り

### Generality Gate 各 cmd zero hit (5/5)

- cmd_413: codd/implementer/chunked_runner.py — stack/framework/domain 名 hardcode ゼロ
- cmd_414: codd/dag/checks/implementation_coverage.py — Next.js 固有用語 hardcode ゼロ
- cmd_415: codd/llm/impl_step_deriver.py — Layer 2 推論内訳は generic 文字列のみ
- cmd_404: codd/repair/verify_runner.py — cmd_398 既存抽象流用、新 hardcode ゼロ
- cmd_405: codd/dag/extractor.py — alias mapping は project declarative、core hash map のみ

### 5 件の caveat/audit 全解消

| 元の問題 | 解消する cmd |
|---|---|
| cmd_412 caveat_1 (timeout) | cmd_413 |
| cmd_412 caveat_2 (C8 patch、最急務) | cmd_414 |
| cmd_412 caveat_3 (Layer 2 ログ) | cmd_415 |
| cmd_398_g1 M_2 (codd-pro routing) | cmd_404 |
| cmd_399 audit item_5 (frontmatter alias) | cmd_405 |

### 1938 tests PASS / SKIP=0

v1.29.0 baseline 1874 → 現状 1938 (+64 tests = 5 cmd 全 PASS)。

### Backwards compatibility

- 5 cmd 関連は完全 opt-in、既存 v1.29.0 ユーザは挙動変化ゼロ
- cmd_413: --chunk-size 不指定で legacy path
- cmd_404: codd-pro 利用可能時は従来通り
- cmd_405: alias mapping 不在で従来通り

### 思想的到達点

cmd_393 declarative + cmd_392/393 検証 + cmd_397 実行 + cmd_398 自己修復 + cmd_406-408
Coverage Closure + cmd_410 Implementation 2-Layer **+ cmd_413-415/404/405 Last-Mile** で
coherence 系が **100% 完成形**に到達。

CoDD は「checklist 駆動 → declarative-coverage 駆動 → best-practice-augmented 駆動 →
**完全無人自動化駆動**」の 4 段階目に到達した。

## [1.29.0] - 2026-05-06

### Added — Implementation Step Derivation 2-Layer (cmd_410)

cmd_393 (declarative) → cmd_392/393 (検証) → cmd_397 (実行) → cmd_398 (自己修復) →
cmd_406-408 (Coverage Closure) に続く第 6 層として、**implementer の実装手順を LLM で
動的展開する 2-layer 機構**を追加。

殿哲学「設計書から実装手順は自動導出するべき」を core 実装。
「interactive 等の特化 layer を予め指定するのは overfitting」を回避し、
**Layer 1 (明示要件展開) + Layer 2 (業界知識からベストプラクティス推論補完)** で
LLM がドメイン非依存に展開する。

### Layer 1 — ImplStepDeriver (明示要件展開)

`design_doc` の機能要件動詞 (例: 「追加できる」「ログイン」) → 実装手順 ImplStep[] を
LLM 動的展開する depth 方向の補完層。cmd_406 PlanDeriver の breadth 展開と直交。

- **新 module**: `codd/llm/impl_step_deriver.py` (ImplStepDeriver ABC + register decorator +
  ImplStep dataclass + SubprocessAiCommandImplStepDeriver builtin)
- **prompt template**: `codd/llm/templates/impl_step_derive_meta.md` (domain-neutral)
- **step catalog hint**: `codd/llm/templates/implementation_step_catalog.yaml`
  (5 ドメイン default: web_app / mobile_app / cli_tool / backend_api / embedded、project_lexicon.yaml で完全 override 可)
- **CLI**: `codd implement plan --task <task_id>` / `codd implement steps`
- **ImplStep.kind は str (open enum)** — Literal 化禁止 (cmd_385 教訓、Web/Mobile/Embedded 多様性吸収)
- **HITL gate**: `approval_mode_per_step_kind` (kind 不在時 default=required、auto は明示宣言必須)

### Layer 2 — BestPracticeAugmenter (ベストプラクティス推論補完)

設計書に**書かれていない**業界標準の関連事項を LLM 知識から動的推論し補完する層。

- **新 module**: `codd/llm/best_practice_augmenter.py` (BestPracticeAugmenter ABC +
  register decorator + builtin)
- **prompt template**: `codd/llm/templates/best_practice_augment_meta.md` (domain-neutral)
- **動作例**: 設計に「ログイン」記述あり → Layer 2 が「ログアウト」「パスワードリセット」
  「セッションタイムアウト」「Remember Me」を自動補完。設計に「データ追加」記述あり →
  「削除」「編集」「検索」「ソート」「ページネーション」を自動補完。
- **対パターンは LLM 業界知識からのみ動的推論**、CoDD core に enum hardcode ゼロ
- **ImplStep schema 拡張**: `inferred: bool` / `confidence: float` /
  `best_practice_category: str (open enum)` を追加
- **CLI**: `codd implement augment`
- **HITL gate 強化**: `layer_2_approval_mode` default=required、auto は
  `require_explicit_optin: true` + `confidence_threshold>=0.9` の **二重 opt-in 必須**

### Implementer Integration

`_execute_task` の prompt 構築段階で Layer 1 (explicit) + Layer 2 (implicit) merged
ImplStep[] を inject。

- **既存 implementer 挙動回帰なし**: `--use-derived-steps=false` で従来動作維持
- **prompt inject 方式 (1 回呼び出し)** — step ごとループ実行 (cmd_411 候補) は将来拡張
- **CLI**: `codd implement run --task <task_id>` (default で derived steps 利用)
- **codd/cli.py +299 LOC**: codd implement plan/steps/augment/run subcommands
- **codd/implementer.py +190 LOC**: prompt inject + Layer 別 approval 統合

### Generality Gate 二層 (継続、6/6 zero hit)

3 module + 3 template = 6 件全て zero hit:

- layer A: `codd/llm/impl_step_deriver.py` + `codd/llm/best_practice_augmenter.py`
- layer B: `codd/llm/templates/{impl_step_derive,best_practice_augment}_meta.md`

forbidden patterns: `button/form/onclick/interaction/interactive/rest/graphql/web app/
mobile/desktop/cli/backend/embedded/ui_input/client_validation/server_handler/db_persist/
next.js/react/django/rails/login/logout/crud/password/session/remember.me`。

**重要**: Layer 2 は「login → logout 推論」のような対パターンを LLM 動的推論する想定で、
これらキーワードを CoDD core code / template text に hardcode したら overfitting 直結。
zero hit 確認で Generality 担保。

### 共通基盤の流用 (重複実装ゼロ)

- `codd/llm/invoke.py` (cmd_397_f) — subprocess + ai_command 抽象を 2 module で import 利用
- `codd/llm/prompt_builder.py` (cmd_397_g) — META_INSTRUCTION + slot 合成
- `codd/llm/approval.py` (cmd_397_h) +58 LOC — Layer 別 approval mode 拡張
- `codd/llm/cache.py` (cmd_397_f) — SHA-256 invalidation pattern

新 SDK 依存ゼロ。

### 1874 tests PASS / SKIP=0

v1.28.0 baseline 1846 → 現状 1874 (+28 tests = cmd_410 全 phase 全 PASS)。

### Backwards compatibility

- `codd.yaml [ai_commands.impl_step_derive]` 不在 → `codd implement plan` はエラー、
  既存 `codd implement` の従来挙動は不変
- `--use-derived-steps=false` で完全に v1.28.0 互換動作
- 既存 v1.28.0 ユーザは挙動変化ゼロ (cmd_410 関連は完全 opt-in)

### 思想的到達点

cmd_393 declarative + cmd_392/393 検証 + cmd_397 実行 + cmd_398 自己修復 +
cmd_406-408 Coverage Closure に加え、本 release で:

- **cmd_410 Layer 1**: 設計書の明示要件動詞を実装手順列に動的展開
- **cmd_410 Layer 2**: 業界知識から暗黙のベストプラクティスを補完

→ 「declarative + LLM 補完 + 実装手順動的導出 + ベストプラクティス推論」で
implementer は **「設計書を読み、書かれていることも書かれていないことも適切に
実装する」** Agent として進化。CoDD は「checklist 駆動 → declarative-coverage 駆動 →
**best-practice-augmented 駆動**」の 3 段階目に到達した。

## [1.28.0] - 2026-05-06

### Added — Coverage Closure Trio (cmd_406 + cmd_407 + cmd_408)

cmd_393 (declarative) → cmd_392/393 (検証) → cmd_397 (実行) → cmd_398 (自己修復) に
続く第 5 層として、**LLM 動的展開 + 双方向抽出 + 動的フルカバー判定** の 3 cmd を bundle release。

「設計→展開→双方向→動的 QC」の閉ループが完成し、CoDD は **checklist 駆動から
declarative-coverage 駆動** へ進化する。LMS で発見された「6 画面実装漏れ」「設計→
implementation_plan task 分解の欠如」「completion_criteria の静的固定」の 3 つの
構造欠陥を構造的に解消する。

### V-MODEL Plan Deriver (cmd_406)

`design_doc` 群を LLM に読ませ、V-MODEL 階層 (要件↔受入テスト / 基本設計↔結合テスト /
詳細設計↔単体テスト + impl) に従って実装すべき task を網羅的に動的展開する機構。

- **新 module**: `codd/llm/plan_deriver.py` (PlanDeriver ABC + register decorator + DerivedTask
  dataclass + SubprocessAiCommandPlanDeriver builtin)
- **prompt template**: `codd/llm/templates/plan_derive_meta.md` (domain-neutral)
- **CLI**: `codd plan derive [--design-doc <path>] [--layer requirement|basic|detailed]`
  / `codd plan show` / `codd plan approve`
- **HITL gate**: required default、cmd_397_h approval gate を流用
- **V-MODEL 層判定**: `design_doc.frontmatter.codd.v_model_layer` declarative > LLM 推論
- **Cache**: `.codd/derived_tasks/{path_safe}.yaml`、SHA-256 invalidation

### Bidirectional Extractor + C8 implementation_coverage (cmd_407)

現状の codd/extractor.py は src/* (実装側) のみ解析する一方向。本 cmd で design_doc 側からも
expected_nodes / expected_edges を LLM 動的抽出する双方向化を実装し、新 C8 check で
「設計期待 vs 実装実態」の差分を構造的に検出する。

- **新 module**: `codd/llm/design_doc_extractor.py` (DesignDocExtractor ABC + 3 dataclass +
  SubprocessAiCommandDesignDocExtractor builtin)
- **新 check**: C8 `implementation_coverage` (`codd/dag/checks/implementation_coverage.py`)
  - violation: `missing_implementation` (red) / `additional_implementation` (amber)
  - severity=red、`block_deploy=False` (v1.28.0 は warning として運用、v1.29.0+ で gate 統合検討)
- **builder 統合**: `codd dag build` で design_doc から ExpectedExtraction を抽出 →
  `design_doc.attributes.expected_extraction` に埋め込み
- **CLI**: `codd extract design --design-doc <path>` / `codd dag verify --check implementation_coverage`
- **Cache**: `.codd/expected_extractions/{path_safe}.yaml`

### Dynamic Full-Coverage Criteria Expander (cmd_408)

task YAML の `completion_criteria` を design_doc + ExpectedExtraction から **動的に
拡張**する機構。軍師 (gunshi) QC が拡充された criteria で PASS 判定し、静的 4 項目チェック
からの脱却を実現する。

- **新 module**: `codd/llm/criteria_expander.py` (CriteriaExpander ABC + 2 dataclass +
  SubprocessAiCommandCriteriaExpander builtin)
- **CriteriaItem source**: static / expected_node / expected_edge / user_journey / v_model
  の 5 種別 dispatch
- **CLI**: `codd qc expand --task <task_id>` / `codd qc evaluate --task <task_id> [--report-json]`
- **軍師 QC workflow**: `codd qc evaluate --report-json` 出力を軍師が手動で yaml report に
  転記する方式 (LLM 拡充項目と軍師判断の境界を明確化)
- **Cache**: `.codd/expanded_criteria/{task_id}.yaml`、cmd_407 ExpectedExtraction の
  dict interface 経由で連携

### Generality Gate 二層 (継続、6/6 zero hit)

3 cmd × 2 layer (A code + B template) = 6 件全て zero hit:

- layer A: codd/llm/{plan_deriver,design_doc_extractor,criteria_expander}.py +
  codd/dag/checks/implementation_coverage.py
- layer B: codd/llm/templates/{plan_derive,design_doc_extract,criteria_expand}_meta.md

forbidden patterns: `claude/openai/gpt/anthropic/screen/page/route/api endpoint/rbac/oauth/
web app/mobile/next.js/react/django/rails/lms/osato-lms`。

### 共通基盤の流用 (重複実装ゼロ)

3 cmd 全てが cmd_397 で確立した LLM 抽象を流用:
- `codd/llm/invoke.py` (cmd_397_f) — subprocess + ai_command 抽象
- `codd/llm/prompt_builder.py` (cmd_397_g) — META_INSTRUCTION + slot 合成
- `codd/llm/parser.py` (cmd_397_g) — JSON schema validation
- `codd/llm/approval.py` (cmd_397_h) — HITL approval gate (cmd_406 が直接利用)
- `codd/llm/cache.py` (cmd_397_f) — SHA-256 invalidation pattern

新 SDK 依存ゼロ。3 cmd 並列実装 (足軽 3 名) で完成。

### 新 DriftEvent kinds: 3

- `plan_derived` (cmd_406 cache invalidation 通知)
- `expected_extraction_derived` (cmd_407)
- `criteria_expanded` (cmd_408)

### Backwards compatibility

- `codd.yaml [ai_commands.plan_derive / design_doc_extract / criteria_expand]` 不在 →
  各 CLI はエラー (明示宣言必須)、既存 codd verify / dag verify は不変
- `.codd/derived_tasks` / `expected_extractions` / `expanded_criteria` 不在 → 各 CLI は空表示
- 既存 v1.27.0 ユーザは挙動変化ゼロ (cmd_406/407/408 関連は完全 opt-in)

### 1846 tests PASS / SKIP=0

v1.27.0 baseline 1763 → 現状 1846 (+83 tests = cmd_406 ~26 + cmd_407 ~28 + cmd_408 ~22 +
parametrize 増分)。

### 思想的到達点

cmd_393 declarative + cmd_392/393 検証 + cmd_397 実行 + cmd_398 自己修復 に加え、
本 release で:
- **cmd_406 動的展開**: LLM が「実装すべき task」を design_doc から自動生成
- **cmd_407 双方向抽出**: LLM が「期待される impl」を design_doc から導出、C8 で coverage 検証
- **cmd_408 動的フルカバー**: LLM が「全画面/全遷移/全要件」を criteria に動的拡充

→ 「設計→展開→双方向→動的 QC」の閉ループが完成し、CoDD は **declarative-coverage 駆動**
の Agent 型 coherence tool に進化した。

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
