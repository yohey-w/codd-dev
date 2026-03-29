# CoDD — Coherence-Driven Development（一貫性駆動開発）

**CoDDは、要件が変わってもAIが構築したシステムの一貫性を保ちます。**

要件と制約を与えるだけ。AIがトップダウンで設計書を生成し、実装戦略とテスト戦略をそこから導出し、依存グラフで変更影響を自動追跡します。何も同期漏れしません。

> *ハーネスはエージェントの動かし方を定義する。CoDDは成果物の一貫性を保つ。*

```
ハーネス (CLAUDE.md, AGENTS.md, Hooks, Skills)  ← ルール、ガードレール、フロー
  └─ CoDD (方法論)                                ← ハーネスの上で動作
       └─ 設計書 (docs/*.md)                      ← CoDDが生成・維持する成果物
```

**パブリックアルファ** — `pip install codd-dev` — init / scan / impact / validate は安定版です。

## 課題

AIは仕様書からコードを生成できる。でも、**プロジェクト途中で要件が変わったら？**

- どの設計書が影響を受ける？
- どのテストを更新すべき？
- どのAPIの契約が壊れた？
- DBマイグレーションの更新を誰か忘れてない？

Spec-driven ツールは「仕様を先に書こう」と教えてくれる。でも、**その仕様が変わった後の追跡はしてくれない。** それがCoDDの出番。

### AGENTS.md やフックだけじゃダメなの？

AGENTS.md、CLAUDE.md、フックは**ハーネス基盤**— エージェントの振る舞いを定義するもの。CoDDはその上に乗る**一貫性レイヤー**で、要件変更時に設計書・実装・テストの同期を保ちます。CoDDはハーネス非依存：Claude Code、GitHub Copilot、Cursor、どのエージェントフレームワークでも動きます。

## 基本原則：設定するな、導出せよ

**上流成果物 + ベストプラクティス = 下流は自明。**

- `system_design.md` に「Next.js + Supabase」→ テスト戦略は vitest + Playwright。設定不要。
- `api_design.md` に「FastAPI」→ pytest + httpx。設定不要。
- 要件が変わった → `codd impact` が影響範囲を正確に表示。

人間が定義するのは要件と制約だけ。AIがそれ以外を全て導出します。

## 動作の仕組み

```
Phase 1: 要件定義 (人間)              ─┐
Phase 2: 設計書生成 (AI)               │ V-Model 左辺
Phase 3: スキャン (自動)               │
Phase 4: 実装 (AI)                    ─┘
Phase 5: 検証 (AI + 人間)             ─── V-Model 右辺
Phase 6: 変更影響分析                  ─┐
Phase 7: 変更伝播                      │ 継続的一貫性
Phase 8: 顧客レビュー                 ─┘
```

設計書は**Wave順**で生成されます — 各Waveは前のWaveに依存：

```
Wave 1: 受入基準 + ADR                  (← 要件のみ)
Wave 2: システム設計                     (← 要件 + Wave 1)
Wave 3: DB設計 + API設計               (← 要件 + Wave 1-2)
Wave 4: UI/UX設計                      (← 要件 + Wave 1-3)
Wave 5: 実装計画                        (← 上記すべて)
```

検証はボトムアップ（IPA共通フレーム準拠）：
```
ユニットテスト    ← 詳細設計を検証
結合テスト       ← システム設計を検証
E2E/システムテスト ← 要件 + 受入基準を検証
```

## 3つのレイヤー（混同しないこと）

```
ハーネス (CLAUDE.md, Hooks, Skills)   ← ルール、ガードレール、フロー
  └─ CoDD (方法論)                     ← ハーネスの上で動作
       └─ 設計書 (docs/*.md)           ← CoDDが生成・維持する成果物
```

- **ハーネス** = エージェントの動かし方（Claude Code, Copilot, Cursor 等）
- **CoDD** = 変更時に成果物の一貫性を保つ方法論
- **設計書** = CoDDが生成・維持するもの

CoDDは**ハーネス非依存**。どのエージェントフレームワークの上でも動きます。

## クイックスタート

```bash
# インストール
pip install codd-dev

# 初期化
codd init --project-name "my-project" --language "typescript"

# スキャン — フロントマターから依存グラフを構築
codd scan

# 影響分析 — この変更で何が壊れる？
codd impact --diff HEAD~1
```

## Claude Code 連携

Claude Codeと相性が良い理由：プロジェクトレベルのフックで、エージェントがファイル編集するたびに依存グラフを自動更新できます。`.claude/settings.json` と `pre-commit` フックを組み合わせれば、スキャンとバリデーションがコーディングループに組み込まれます。

推奨 Claude Code スキル：

- `codd-init`
- `codd-scan`
- `codd-impact`
- `codd-validate`
- `codd-generate`

詳細は [docs/claude-code-setup.md](docs/claude-code-setup.md) を参照。

## 実プロジェクト実績：the-clientLMS

CoDDは実稼働のLMS（学習管理システム）で実証済み。設計書・実装コード・テストの全てをAIがCoDDワークフローに従って生成。18のMarkdownファイルが依存グラフで接続されています。

```
docs/
├── requirements/       # 何を作るか（顧客合意、SSoT）
├── design/             # どう作るか（システム設計、API、DB、UI）
├── detailed_design/    # モジュールレベルの仕様
├── plan/               # WBS、スケジュール、RACI
├── governance/         # ADR、議事録、変更要求
├── test/               # 受入基準、テスト計画
├── operations/         # 運用手順書、監視設計
└── infra/              # インフラ設計
```

設計書はCoDDフロントマターで依存関係を宣言：

```yaml
---
codd:
  node_id: "design:api-design"
  depends_on:
    - id: "design:system-design"
      relation: derives_from
    - id: "req:lms-requirements-v2.0"
      relation: implements
---
```

プロジェクト途中で要件が変更された際、`codd impact` が影響を受ける設計書・APIエンドポイント・テストケースを正確に特定 — AIが自動で修正しました。

## Spec Kit / OpenSpec との違い

|  | Spec Kit | OpenSpec | **CoDD** |
|--|----------|---------|----------|
| 仕様を先に書く | Yes | Yes | Yes |
| AIが仕様からコード生成 | Yes | Yes | Yes |
| **変更伝播** | No | No | **依存グラフ + 影響分析** |
| **テスト戦略をアーキテクチャから導出** | No | No | **自動（設定不要）** |
| **V-Model 検証** | No | No | **Unit → Integration → E2E** |
| **変更時の影響分析** | No | No | **codd impact --diff HEAD~1** |
| ハーネス非依存 | GitHub Copilot寄り | マルチエージェント | **どのハーネスでも** |

**Spec Kit と OpenSpec は「どう始めるか」に答える。CoDDは「途中で変わった時にどうするか」に答える。**

## 現在利用可能な機能 (v0.2.0-alpha.1)

| コマンド | ステータス | 機能 |
|---------|--------|-------------|
| `codd init` | **安定版** | プロジェクト初期化 |
| `codd scan` | **安定版** | フロントマターから依存グラフ構築 |
| `codd impact` | **安定版** | 変更影響分析（Green/Amber/Gray バンド） |
| `codd validate` | **アルファ** | フロントマター整合性チェック |
| `codd generate` | 実験的 | Wave順で設計書生成 |
| `codd plan` | 実験的 | Wave実行状況 |
| `codd verify` | 実験的 | V-Model検証（型チェック + テスト → 設計追跡） |
| `codd implement` | 実験的 | 設計書 → コード生成 |

## フロントマターが唯一の信頼できるソース

CoDDはMarkdownファイルのYAMLフロントマターで依存関係を宣言します。`graph.db` は導出されたキャッシュ — `codd scan` のたびに再生成されます。別途設定ファイルを管理する必要はありません。

```yaml
---
codd:
  node_id: "design:system-design"
  type: design
  depends_on:
    - id: "req:lms-requirements-v2.0"
      relation: implements
  conventions:
    - targets: ["db:rls_policies"]
      reason: "テナント分離は非交渉事項"
---
```

## ロードマップ

- [ ] セマンティック依存関係タイプ (requires, affects, verifies, implements)
- [ ] `codd verify` — 設計書 ↔ コード ↔ テストの完全一貫性チェック
- [ ] マルチエージェント連携例 (Claude Code, Copilot, Cursor)
- [ ] VS Code 拡張（影響分析の可視化）

## 詳細な解説記事

- [Zenn: CoDD — Coherence-Driven Development](https://zenn.dev/shio_shoppaize/articles/shogun-codd-coherence)
- [dev.to: CoDD — What Happens After "Spec First"](https://dev.to/yohey-w/codd-coherence-driven-development-what-happens-after-spec-first-514f)

## ライセンス

MIT
