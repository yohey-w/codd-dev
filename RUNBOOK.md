# CoDD Runbook

## Overview

CoDD (Coherence-Driven Development) は、V-Modelの理想をAI時代に完遂する開発手法。要件定義を起点に設計→実装→検証の全成果物を依存グラフで追跡し、変更影響を自動伝播する。

**人間がやること**: 要件定義と判断（舵を取る）
**AIがやること**: 設計書・コード・テスト — 全部生成・全部更新
**CoDDがやること**: 何に影響するか教える。上流から下流を導出する

### 核心

**要件定義と制約条件をインプットにして、必要最小限のハーネスとそのときのベストプラクティスに応じて、設計・実装・テストまで全部やる。**

- インプットは2つだけ: 要件定義 + 制約条件
- ハーネスは必要最小限: 過剰な設定・宣言は不要。上流が決まれば下流は自明
- アウトプットは全部: 設計書 → コード → テスト
- アーキテクチャが決まればテスト手法も自ずと決まる。ツール指定は不要
- やるのはAI。人間は要件と制約を決めるだけ

### Phase全体像

```
Phase 0: 初期導入
Phase 1: 要件定義（人間）          ─┐
Phase 2: 設計書生成（AI — Wave制）  │ V-Model 左側（設計）
Phase 3: スキャン（自動）           │
Phase 4: 実装（AI）                ─┘ V-Model 底部
Phase 5: 検証（AI + 人間）         ─── V-Model 右側（検証）
Phase 6: 変更影響分析（自動）       ─┐
Phase 7: 変更伝播（AI + 人間）      │ 継続的整合性維持
Phase 8: 顧客説明（人間）          ─┘
```

## MECE Document Structure

```
docs/
├── requirements/      # What  — 何を作るか（顧客合意、SSoT）
├── design/            # How   — どう作るか（技術判断の証跡）
├── plan/              # When  — いつ誰がやるか（WBS、RACI）
├── governance/        # Why   — なぜその判断か（ADR、議事録、変更要求）
│   ├── meeting_minutes/
│   └── change_requests/
├── test/              # Verify — 正しく作ったか（テスト計画・結果）
└── operations/        # Run   — どう運用するか（運用手順・監視設計）
```

## Workflow

### Phase 0: 初期導入（1回だけ）

```bash
codd init --project-name "my-project" --language "typescript"
```

1. `codd/codd.yaml` を編集: `doc_dirs` に上記6カテゴリを設定
2. 要件定義書を `docs/requirements/` に配置
3. 要件定義書にCoDDフロントマターを埋め込む

### Phase 1: 要件定義（人間）

要件定義書を作成し、フロントマターで依存関係を宣言する:

```yaml
---
codd:
  node_id: "req:lms-requirements-v2.0"
  type: requirement
  depends_on:
    - id: "design:course-service"
      relation: specifies
    - id: "db_table:courses"
      relation: requires
  conventions:
    - targets: ["db:rls_policies", "test:test_tenant_isolation"]
      reason: "テナント分離は絶対条件"
  data_dependencies:
    - table: tenants
      column: status
      affects: ["module:auth"]
      condition: "停止で全API拒否"
---

# 要件定義書本文（人間が読む部分）
```

### Phase 2: 設計書生成（AI — Wave制）

要件定義を起点にAIが設計書を生成する。各設計書にもフロントマターを埋め込む。

**依存関係がWave順序を決める。並行可能なのは同一Wave内のみ。**

```
Wave 1: 要件のみに依存（並行可能）
  ├── acceptance_criteria.md  ← 要件をテスト可能な条件に変換
  └── decisions.md            ← 要件に記載済みの技術判断を記録

Wave 2: 要件 + Wave 1に依存
  └── system_design.md        ← アーキテクチャ（サービス境界が決まる）

Wave 3: Wave 2に依存（並行可能）
  ├── database_design.md      ← サービス境界からテーブル設計
  └── api_design.md           ← サービス境界からAPI仕様（DB設計と部分並行可）

Wave 4: Wave 3に依存
  └── ui_ux_design.md         ← API仕様からUI設計

Wave 5: Wave 1-4全てに依存
  └── implementation_plan.md  ← 全設計からWBS・スケジュール
```

| Wave | ファイル | 配置先 | node_id | 依存先 |
|------|----------|--------|---------|--------|
| 1 | acceptance_criteria.md | docs/test/ | design:acceptance-criteria | req のみ |
| 1 | decisions.md | docs/governance/ | governance:decisions | req のみ |
| 2 | system_design.md | docs/design/ | design:system-design | req + Wave1 |
| 3 | database_design.md | docs/design/ | design:database-design | req + Wave1-2 |
| 3 | api_design.md | docs/design/ | design:api-design | req + Wave1-2 |
| 4 | ui_ux_design.md | docs/design/ | design:ui-ux-design | req + Wave1-3 |
| 5 | implementation_plan.md | docs/plan/ | design:implementation-plan | req + Wave1-4 |

**Wave内は並行実行OK。Wave間はHITL（Human-in-the-Loop）ゲートを通過してから次に進む。**

#### HITL Gate（Wave間の人間レビュー）

```
Wave N 完了
  ↓
codd scan → グラフ更新
  ↓
人間がレビュー（成果物の品質・方向性を確認）
  ↓
承認 → Wave N+1 開始
却下 → AIが修正 → 再レビュー
```

**なぜHITLが必要か:**
1. 上流の誤りは下流に増幅する（Wave 2が間違えばWave 3-5全滅）
2. 早期の軌道修正はコストが安い
3. 顧客との合意ポイントになる（「ここまで合意」の証跡）
4. AIが要件定義にない判断をしていないか検証する

**生成ルール**:
1. 要件定義書を全文読み込む
2. 前Waveの成果物が存在する場合はそれも読み込む
3. 各設計書のフロントマターには `depends_on` で依存先への参照を含める
4. `conventions` で設計上の制約（不変条件）を宣言する
5. 本文は人間が読める形式で書く
6. 要件定義書に書いていない機能を勝手に追加しない

### Phase 3: スキャン（自動）

```bash
codd scan --path .
```

全ドキュメントのフロントマターを読み取り、`graph.db` を再構築する。
- 自動生成データは毎回リフレッシュ
- 人間が追加した暗黙知（source_type=human）は保持

### Phase 4: 実装（AI）

設計書に基づきAIがソースコード・単体テストを生成。コミット時にgraph.dbが更新される。

### Phase 5: 検証 — V-Model右側（AI + 人間）

V-Modelの左側（設計）が完了したら、右側（検証）を**下位から上位へ**昇順で実行する。

#### V-Model対応表（IPA 共通フレーム準拠）

```
左側（設計）                        右側（検証）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
要件定義                      ←→  システムテスト / 受入テスト
  基本設計（system_design）   ←→  結合テスト（統合テスト）
    詳細設計（API/DB設計）    ←→  単体テスト
      実装                    ←→  コード作成・単体テスト実行
```

| 設計書 | 検証レベル | 検証内容 |
|--------|-----------|---------|
| 詳細設計（api_design, database_design） | **単体テスト** | 個々のモジュール・関数が詳細設計通りに動くか |
| 基本設計（system_design） | **結合テスト** | モジュール間のインターフェースが基本設計通りに連携するか |
| 要件定義 + 受入条件 | **システムテスト / E2Eテスト** | システム全体が要件定義・受入条件を満たすか |

#### 導出原則: テスト手法は設計書から自明

**テストツール・フレームワークを手動で設定する必要はない。**

system_design.mdにアーキテクチャが記載された時点で、ベストプラクティスに基づきテスト手法は自動的に決まる:

| system_design.mdの記載 | 導出されるテスト手法 |
|------------------------|---------------------|
| Next.js + TypeScript | vitest（単体）、Playwright（E2E） |
| FastAPI + Python | pytest（単体・結合）、httpx（APIテスト） |
| React Native | Jest（単体）、Detox or Appium（E2E） |
| Spring Boot + Java | JUnit（単体）、REST Assured（API）、Selenium（E2E） |
| CLI tool（Go） | go test（単体・結合）、bats or go test（E2Eはコマンド実行テスト） |
| API only（RESTful） | 言語のテストFW（単体）、API client（結合・E2E） |

**AIが上流ドキュメントを読めば下流は導出できる。** これはテストに限らずCoDDの根本原則。

#### 検証Wave（実装完了後に昇順実行）

```
検証Wave 1: 単体テスト
  ├── 詳細設計の各モジュールに対するテスト実行
  └── カバレッジ確認

検証Wave 2: 結合テスト
  ├── モジュール間連携テスト（API ↔ DB、サービス間通信）
  └── 基本設計のインターフェース仕様との照合

検証Wave 3: システムテスト / E2Eテスト
  ├── 受入条件（acceptance_criteria.md）の各項目を検証
  └── 要件定義の機能要件・非機能要件を網羅的にテスト
```

**検証Wave間もHITLゲートを通す。** 単体テストが全パスしてから結合テストに進む。

#### 検証完了条件

| レベル | 完了条件 |
|--------|---------|
| 単体テスト | 全テストパス。SKIP=0。 |
| 結合テスト | 全テストパス。モジュール間のデータフロー検証済み。 |
| システムテスト / E2E | acceptance_criteria.mdの全項目がパス。 |

**全レベルの検証が完了して初めて「開発完了」。** テストが通っていない成果物は未完成。

### Phase 6: 変更影響分析（自動）

要件や設計に変更があったら（検証完了後でも運用中でも）:

```bash
codd impact --diff HEAD~1 --path .
```

出力:
- **Convention Alerts**: 不変条件に触れる変更（最重要）
- **Green Band**: 高確信度の影響先（自動伝播OK）
- **Amber Band**: 要レビュー（人間が判断）
- **Gray Band**: 参考情報

### Phase 7: 変更伝播（AI + 人間）

1. impactレポートを見て影響先を確認
2. AIが影響先の設計書・コード・テストを更新
3. 人間が判断すべき箇所だけ確認・承認
4. コミット → Phase 3に戻る
5. **変更が実装に影響する場合、Phase 5（検証）を再実行**

### Phase 8: 顧客説明（人間）

変更要求があった場合:
1. `codd impact` で影響範囲を特定
2. レポートを `docs/governance/change_requests/` に保存
3. 顧客に「この変更でここが影響します」と提示
4. 合意後、Phase 6で伝播

## Multi-Agent Operation（将軍システム連携）

CoDDは単体でも機能するが、マルチエージェントシステムと組み合わせると設計書生成・変更伝播を自動化できる。以下は [orchestrator](https://github.com/yohey-w/orchestrator) での運用例。

### エージェント構成

```
殿（人間） — 要件定義・HITL判断・顧客折衝
  ↓
将軍（Shogun） — CoDD全体統制、Waveコマンド発行
  ↓
家老（Karo） — タスク分解・足軽アサイン・品質管理
  ↓
足軽（Ashigaru）×N — 設計書生成・コード実装・テスト作成
  ↑
軍師（Gunshi） — 品質チェック・戦略レビュー
```

### Wave実行フロー

```
将軍: cmd_236 を shogun_to_karo.yaml に書く
  ↓
将軍: inbox_write.sh で家老に通知
  ↓
家老: コマンドYAMLを読み、subtask YAMLを作成
家老: 同一Wave内のsubtaskを足軽に並行アサイン
  ↓
足軽: 要件定義 + RUNBOOK.md を全文読む
足軽: 設計書を生成（CoDDフロントマター付き）
足軽: 完了報告 → 家老
  ↓
家老: 成果物を確認、必要なら軍師にQCレビュー依頼
家老: Wave N 完了を将軍に報告
  ↓
将軍: codd scan → グラフ更新
将軍: 殿にHITLレビューを依頼
  ↓
殿: レビュー → 承認 or 却下
  ↓
承認 → 将軍が Wave N+1 のコマンドを発行
却下 → 将軍が修正指示を家老に投げる
```

### コマンドYAML設計（Wave制）

```yaml
- id: cmd_236
  status: pending
  north_star: the-clientLMS設計書をCoDD Wave 1で生成
  type: parallel_batch
  context: |
    ■ SSoT: /path/to/requirements.md
    ■ RUNBOOK: /path/to/RUNBOOK.md（足軽にも必ず読ませよ）
    ■ Wave構成とHITLゲートの説明
    ■ フロントマター仕様・命名規則
  subtasks:
    - id: subtask_236a
      title: "受入条件定義"
      output_path: "docs/test/acceptance_criteria.md"
      instructions: |
        要件定義を読み、BDD形式で受入条件を書く。
        CoDDフロントマター必須。
    - id: subtask_236b
      title: "意思決定記録"
      output_path: "docs/governance/decisions.md"
      instructions: |
        要件定義から既決事項をADR形式で記録する。
        CoDDフロントマター必須。
```

### Wave間のHITL運用

| Step | Who | Action |
|------|-----|--------|
| 1 | 将軍 | `codd scan` → グラフ更新 |
| 2 | 将軍 | `codd impact` → 整合性チェック |
| 3 | 将軍 | 殿にレビュー依頼（成果物サマリ + impactレポート） |
| 4 | 殿 | レビュー → 承認 / 修正指示 |
| 5 | 将軍 | 承認なら次Waveコマンド発行、却下なら修正コマンド発行 |

### 変更伝播の運用

要件変更が発生した場合:

```
殿: 要件定義書を更新
  ↓
将軍: codd impact → 影響レポート生成
将軍: 影響先の設計書更新コマンドを家老に投げる
  ↓
家老: 影響先ドキュメントごとにsubtask作成
家老: 足軽に「この設計書のここを更新しろ」と指示
  ↓
足軽: 要件定義 + 既存設計書 + impactレポートを読む
足軽: 設計書を更新（フロントマターも更新）
  ↓
将軍: codd scan → グラフ再構築
将軍: 殿にHITLレビュー依頼
```

### 足軽への指示テンプレート

足軽が確実にCoDD準拠の成果物を出すために、タスクYAMLに以下を含める:

```
【前提】
1. /path/to/RUNBOOK.md を全文読め
2. /path/to/requirements.md を全文読め
3. 前Waveの成果物があれば読め

【タスク】
設計書を生成する。

【CoDDフロントマター】
node_id: "design:xxx"
depends_on: [具体的なノードID]
conventions: [不変条件]

【最重要ルール】
1. 要件定義書は変更するな
2. フロントマターを必ず付けろ
3. 要件にない機能を追加するな
```

## Frontmatter Reference

### node_id naming convention

| Prefix | Type | Example |
|--------|------|---------|
| `req:` | 要件 | `req:lms-requirements-v2.0` |
| `design:` | 設計 | `design:system-design` |
| `db_table:` | テーブル | `db_table:users` |
| `db:` | DB構造体 | `db:rls_policies` |
| `endpoint:` | API | `endpoint:POST /auth/login` |
| `module:` | モジュール | `module:auth` |
| `file:` | ソースファイル | `file:src/auth.ts` |
| `test:` | テストケース | `test:test_tenant_isolation` |
| `config:` | 設定 | `config:bunny_stream` |
| `infra:` | インフラ | `infra:worm_storage` |
| `governance:` | 統治 | `governance:decisions` |

### relation types

| Relation | Direction | Meaning |
|----------|-----------|---------|
| `specifies` | req → design | 要件が設計を規定 |
| `implements` | design → req | 設計が要件を実装 |
| `derives_from` | child → parent | 派生元 |
| `requires` | any → any | 依存 |
| `defines` | doc → entity | ドキュメントがエンティティを定義 |
| `consumes` | ui → api | UIがAPIを利用 |
| `schedules` | plan → design | 計画が設計を日程化 |
| `satisfies` | design → criteria | 設計が受入条件を満たす |
| `tests` | criteria → req | 受入条件が要件をテスト |
| `must_review` | any → any | 変更時にレビュー必須（convention） |
| `imports` | file → file | ソースコードのimport |
| `reads_table` | file → db | コードがテーブルを読む |
| `writes_table` | file → db | コードがテーブルに書く |
| `behavioral_dependency` | data → code | データ変更でコードの挙動が変わる |

### Evidence source_types

| source_type | Auto-purge | Description |
|-------------|-----------|-------------|
| `frontmatter` | Yes | ドキュメントのYAMLフロントマターから |
| `static` | Yes | ソースコードの静的解析（import等） |
| `framework` | Yes | フレームワーク知識（Next.js等） |
| `inferred` | Yes | AI推論 |
| `human` | **No** | 人間の暗黙知（手動追加） |
| `dynamic` | **No** | 実行時トレース |
| `history` | **No** | 変更履歴からの学習 |

## Git Integration

- 全成果物はGitで管理: `git log` = いつ何が変わったか
- `codd impact` が補完: **なぜ変わったか、何に影響するか**
- `graph.db` は `.gitignore` に入れる（スキャンで再生成可能）
- フロントマター = データ。ドキュメント本文 = 人間向け。物理的に同じファイル。
