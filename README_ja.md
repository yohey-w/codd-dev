<p align="center">
  <strong>CoDD — Coherence-Driven Development（一貫性駆動開発）</strong><br>
  <em>要件が変わっても、AIが構築したシステムの一貫性を保つ。</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/v/codd-dev?style=flat-square&color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/pyversions/codd-dev?style=flat-square" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License"></a>
  <a href="https://github.com/yohey-w/codd-dev/stargazers"><img src="https://img.shields.io/github/stars/yohey-w/codd-dev?style=flat-square" alt="Stars"></a>
</p>

<p align="center">
  日本語 | <a href="README.md">English</a>
</p>

---

> *ハーネスはエージェントの動かし方を定義する。CoDDは成果物の一貫性を保つ。*

```
pip install codd-dev
```

**パブリックアルファ** — `init` / `scan` / `impact` は安定版、`validate` はアルファです。

---

## なぜCoDD？

AIは仕様書からコードを生成できる。でも、**プロジェクト途中で要件が変わったら？**

- どの設計書が影響を受ける？
- どのテストを更新すべき？
- どのAPIの契約が壊れた？
- DBマイグレーションの更新を誰か忘れてない？

**Spec Kit** と **OpenSpec** は「どう始めるか」に答える。
**CoDD** は「途中で変わった時にどうするか」に答える。

## 動作の仕組み

```
要件定義 (人間)  →  設計書生成 (AI)  →  コード & テスト (AI)
                          ↑
                  codd scan が
                   依存グラフを構築
                          ↓
            何か変わった？ codd impact が
             影響範囲を正確に特定 — 自動で。
```

### 3つのレイヤー

```
ハーネス (CLAUDE.md, Hooks, Skills)   ← ルール、ガードレール、フロー
  └─ CoDD (方法論)                     ← 変更時の一貫性維持
       └─ 設計書 (docs/*.md)           ← CoDDが管理する成果物
```

CoDDは**ハーネス非依存** — Claude Code、Copilot、Cursor、どのエージェントフレームワークでも動きます。

## 基本原則：設定するな、導出せよ

| アーキテクチャ | 導出されるテスト戦略 | 設定は？ |
|---|---|---|
| Next.js + Supabase | vitest + Playwright | 不要 |
| FastAPI + Python | pytest + httpx | 不要 |
| CLI tool in Go | go test | 不要 |

**上流が下流を決定する。** 要件と制約を定義するだけ。AIがそれ以外を全て導出します。

## クイックスタート

```bash
pip install codd-dev
mkdir my-project && cd my-project && git init

# 初期化 — 要件定義ファイルを渡すだけ（形式自由: txt, md, doc）
codd init --project-name "my-project" --language "typescript" \
  --requirements spec.txt

# AIが設計書を生成（wave_configも自動生成）
codd generate --wave 2

# 依存グラフ構築 → 影響分析
codd scan
codd impact
```

## 5分で体験するデモ

**TaskFlow**（タスク管理アプリ）を題材に、**要件定義を平文で書いて**、残りは全部CoDD+AIに任せる。

### Step 1: 要件定義を書く（平文でOK — txt, md, doc 何でもいい）

```text
# TaskFlow — 要件定義

## 機能要件
- ユーザー認証（メール + Google OAuth）
- ワークスペース管理（チーム、ロール、招待）
- タスクCRUD（担当者、ラベル、期限）
- リアルタイム更新（WebSocket）
- ファイル添付（S3）
- 通知システム（アプリ内 + メール）

## 制約
- Next.js + Prisma + PostgreSQL
- ワークスペース分離はRLSで実現
- 全APIエンドポイントにレートリミット
```

`spec.txt` として保存。これだけ。特別なフォーマットは不要。

### Step 2: CoDDを初期化（要件定義を渡す）

```bash
pip install codd-dev
mkdir taskflow && cd taskflow && git init
codd init --project-name "taskflow" --language "typescript" \
  --requirements spec.txt
```

CoDDがフロントマター（`node_id`, `type`, 依存メタデータ）を自動付与。人間は触らない。

### Step 3: AIが設計書を生成

`codd generate` はAIを呼び出して、要件から設計書をWave順に生成する。`wave_config` が無ければ要件から自動生成される。

```bash
codd generate --wave 2   # システム設計・API設計を生成
codd generate --wave 3   # DB設計・認証設計を生成
codd generate --wave 4   # テスト戦略を生成
```

### Step 4: 依存グラフを構築

```bash
codd scan
```

```
Frontmatter: 7 documents in docs
Scan complete:
  Documents with frontmatter: 7
  Graph: 7 nodes, 15 edges
  Evidence: 15 total (0 human, 15 auto)
```

7本の設計書から15本のエッジ。設定ファイルゼロ。

### Step 5: 要件を変えて影響分析

PMから「SSO対応と監査ログ追加して」と言われた。`docs/requirements/requirements.md` を開いて2行追加:

```text
## 追加要件（v1.1）
- SAML SSO対応（エンタープライズ顧客向け）
- 監査ログ（全操作の記録・エクスポート）
```

ファイルを保存して、CoDDに「何が影響受ける？」と聞く:

```bash
codd impact    # 未コミットの変更を自動検知
```

```
Changed files: 1
  - docs/requirements/requirements.md → req:taskflow-requirements

# CoDD Impact Report

## Green Band (high confidence, auto-propagate)
| Target                  | Depth | Confidence |
|-------------------------|-------|------------|
| design:system-design    | 1     | 0.90       |
| design:api-design       | 1     | 0.90       |
| detail:db-design        | 2     | 0.90       |
| detail:auth-design      | 2     | 0.90       |

## Amber Band (must review)
| Target                  | Depth | Confidence |
|-------------------------|-------|------------|
| test:test-strategy      | 2     | 0.90       |

## Gray Band (informational)
| Target                  | Depth | Confidence |
|-------------------------|-------|------------|
| plan:implementation     | 2     | 0.00       |
```

**2行変えただけで7本中6本の設計書が影響を受けると判定。** Green帯域はAIが自動更新。Amber帯域は人間に提示。変更が怖くなくなる。

## Wave順生成

設計書は依存関係の順序で生成されます — 各Waveは前のWaveに依存：

```
Wave 1  受入基準 + ADR                ← 要件のみ
Wave 2  システム設計                   ← 要件 + Wave 1
Wave 3  DB設計 + API設計             ← 要件 + Wave 1-2
Wave 4  UI/UX設計                    ← 要件 + Wave 1-3
Wave 5  実装計画                      ← 上記すべて
```

検証はボトムアップ（V-Model）：

```
ユニットテスト    ← 詳細設計を検証
結合テスト       ← システム設計を検証
E2E/システムテスト ← 要件 + 受入基準を検証
```

## フロントマター = 唯一の信頼できるソース

依存関係はMarkdownのフロントマターで宣言。別途設定ファイルは不要。

```yaml
---
codd:
  node_id: "design:api-design"
  depends_on:
    - id: "design:system-design"
      relation: derives_from
    - id: "req:my-project-requirements"
      relation: implements
---
```

`codd/scan/` はキャッシュ — `codd scan` のたびに再生成されます。

## ブラウンフィールド？ ここから

既存コードベースがある場合、`codd extract` がソースコードから設計書を逆生成します。AI不要——純粋な静的解析。

```bash
cd existing-project
codd extract
```

```
Extracted: 13 modules from 45 files (12,340 lines)
Output: codd/extracted/
  system-context.md     # モジュールマップ + 依存グラフ
  modules/auth.md       # モジュール別設計書
  modules/api.md
  modules/db.md
  ...
```

**設計思想**: V-Modelにおいて、意図は要件定義にのみ存在する。アーキテクチャ・詳細設計・テストは構造事実——コードから抽出可能。`codd extract` は構造を取り、「なぜ」は後から人が加える。

```bash
# 生成された文書をレビューし、確認済みのものを昇格
mv codd/extracted/modules/auth.md docs/design/
# 依存グラフを構築
codd scan
codd impact
```

## コマンド

| コマンド | ステータス | 説明 |
|---------|--------|-------------|
| `codd init` | **安定版** | プロジェクト初期化 |
| `codd scan` | **安定版** | フロントマターから依存グラフ構築 |
| `codd impact` | **安定版** | 変更影響分析（Green / Amber / Gray バンド） |
| `codd validate` | **アルファ** | フロントマター整合性 & グラフ一貫性チェック |
| `codd generate` | 実験的 | Wave順で設計書生成 |
| `codd plan` | 実験的 | Wave実行状況 |
| `codd verify` | 実験的 | V-Model検証 |
| `codd implement` | 実験的 | 設計書→コード生成 |
| `codd extract` | **アルファ** | 既存コードから設計書を逆生成 |

## Claude Code 連携

CoDDはClaude Code用のスラッシュコマンドSkillを同梱。CLIを直接叩く代わりに、Skillを使えばClaudeがプロジェクトの状態を読み取って適切なコマンドを実行する。

### Skills デモ — 同じTaskFlowアプリ、CLIコマンド不要

```
殿:  /codd-init
     → Claude: codd init --project-name "taskflow" --language "typescript" \
                 --requirements spec.txt

殿:  /codd-generate
     → Claude: codd generate --wave 2 --path .
     → Claude が生成された設計書を読み、スコープ確認、フロントマター検証
     → 「Wave 2の設計書を確認しました。Wave 3に進みますか？」

殿:  はい

殿:  /codd-generate
     → Claude: codd generate --wave 3 --path .

殿:  /codd-scan
     → Claude: codd scan --path .
     → 報告: 「7ドキュメント、15エッジ。警告なし。」

殿:  （要件を編集 — SSO対応と監査ログを追加）

殿:  /codd-impact
     → Claude: codd impact --path .
     → Green帯域: system-design、api-design、db-design、auth-designを自動更新
     → Amber帯域: 「test-strategyが影響を受けています。更新しますか？」
```

**CLIとの違い**: Skillはhuman-in-the-loopゲートを追加する。`/codd-generate` はWave間で承認を求めて停止。`/codd-impact` はGreen/Amber/Grayプロトコルに従い、安全な変更は自動更新、リスクのある変更は確認してから実行。

### フック連携 — 一度設定したら、もう意識しなくていい

このフックを入れれば、**`codd scan` を手動で叩く必要は二度とない。** ファイル編集のたびに自動実行 — 依存グラフは常に最新、常に正確、意識ゼロ：

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{
        "type": "command",
        "command": "codd scan --path ."
      }]
    }]
  }
}
```

フックが有効なら、やることは**普通にファイルを編集して、影響を知りたくなったら `/codd-impact` を叩く。それだけ。** グラフのメンテナンスは完全に透明。

### 利用可能なSkill

| Skill | 機能 |
|-------|------|
| `/codd-init` | 初期化 + 要件インポート |
| `/codd-generate` | HITLゲート付きWave順設計書生成 |
| `/codd-scan` | 依存グラフ再構築 |
| `/codd-impact` | Green/Amber/Grayプロトコルで変更影響分析 |
| `/codd-validate` | フロントマター & 依存関係の整合性チェック |

詳細は [docs/claude-code-setup_ja.md](docs/claude-code-setup_ja.md) を参照。

## 比較

|  | Spec Kit | OpenSpec | **CoDD** |
|--|----------|---------|----------|
| 仕様を先に書く | Yes | Yes | Yes |
| **変更伝播** | No | No | **依存グラフ + 影響分析** |
| **テスト戦略の自動導出** | No | No | **アーキテクチャから自動** |
| **V-Model検証** | No | No | **Unit → Integration → E2E** |
| **影響分析** | No | No | **`codd impact`** |
| ハーネス非依存 | Copilot寄り | マルチエージェント | **どのハーネスでも** |

## 実プロジェクト実績

本番Webアプリで実証済み — 18の設計書が依存グラフで接続。設計書・コード・テストの全てをAIがCoDDに従って生成。プロジェクト途中で要件が変更された際、`codd impact` が影響を受ける成果物を特定し、AIが自動で修正。

```
docs/
├── requirements/       # 何を作るか（人間の入力）
├── design/             # システム設計、API、DB、UI（6ファイル）
├── detailed_design/    # モジュールレベルの仕様（4ファイル）
├── governance/         # ADR（3ファイル）
├── plan/               # 実装計画
├── test/               # 受入基準、テスト戦略
├── operations/         # 運用手順書
└── infra/              # インフラ設計
```

## ロードマップ

- [ ] セマンティック依存関係タイプ (`requires`, `affects`, `verifies`, `implements`)
- [x] `codd extract` — 既存コードベースから設計書を逆生成（ブラウンフィールド対応）
- [ ] `codd verify` — 設計書↔コード↔テストの完全一貫性チェック
- [ ] マルチハーネス連携例（Claude Code, Copilot, Cursor）
- [ ] VS Code拡張（影響分析の可視化）

## 解説記事

- [Zenn（日本語）: CoDD 詳細解説](https://zenn.dev/shio_shoppaize/articles/shogun-codd-coherence)
- [dev.to（英語）: What Happens After "Spec First"](https://dev.to/yohey-w/codd-coherence-driven-development-what-happens-after-spec-first-514f)

## ライセンス

MIT
