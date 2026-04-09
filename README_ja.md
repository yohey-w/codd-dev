<p align="center">
  <strong>CoDD — Coherence-Driven Development（一貫性駆動開発）</strong><br>
  <em>AI支援開発における変更管理のエビデンスエンジン。</em>
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

> *コードが変わったとき、CoDDは影響範囲を追跡し、違反を検出し、マージ判断のためのエビデンスを生成する。*

```
pip install codd-dev
```

**v1.6.0** — `init` / `scan` / `impact` は安定版。`propagate` でコード変更を下流設計書に伝搬。`extract --ai` にbaselineプリセット搭載。OSS/Proブリッジパターン分離。GitHub Action によるCI連携対応。

---

## なぜCoDD？

AIは仕様を書ける。でも**上流が変わったら？**

あらゆるSpec-firstツールは「作る」ところで止まる。CoDDはそこから始まる。要件が変わったとき、コードが更新されたとき、設計の前提が崩れたとき、CoDDが**変更を下流に自動伝搬**する — 影響を受ける設計書を更新し、古くなった成果物をフラグし、エビデンスの証跡を残す。

```
要件が変わった → codd impact が影響設計書6本を特定
コードが変わった → codd propagate が下流設計書を更新
設計が変わった → CEGグラフが全依存先を追跡
```

他のツールにはこれができない。spec-kit、Kiro、cc-sddは文書を作る。**CoDDは文書の一貫性を維持する。**

## 動作の仕組み

```
要件定義 (人間)  →  設計書生成 (AI)  →  コード & テスト (AI)
       ↕                   ↕                    ↕
   codd impact       codd propagate        codd extract
  (何が変わった？)    (下流を更新)         (逆生成)
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

### グリーンフィールド（新規プロジェクト）

```bash
pip install codd-dev
mkdir my-project && cd my-project && git init

# 初期化 — 要件定義ファイルを渡すだけ（形式自由: txt, md, doc）
codd init --project-name "my-project" --language "typescript" \
  --requirements spec.txt

# AIが設計書の依存グラフを設計
codd plan --init

# 設計書をwave順に生成
waves=$(codd plan --waves)
for wave in $(seq 1 $waves); do
  codd generate --wave $wave
done

# 品質ゲート — AIの手抜きを検出（TODO、プレースホルダー）
codd validate

# 設計書からコード生成
sprints=$(codd plan --sprints)
for sprint in $(seq 1 $sprints); do
  codd implement --sprint $sprint
done

# コード断片をビルド可能なプロジェクトに統合
codd assemble
```

### ブラウンフィールド（既存プロジェクト）

```bash
codd extract              # 既存コードから設計書を逆生成
codd require              # コードから要件を推論（何が作られ、なぜか）
codd plan --init          # 抽出結果からwave_config生成
codd scan                 # 依存グラフ構築
codd impact               # 変更影響分析
codd audit --skip-review  # 変更レビュー一括実行: validate + impact + policy
codd measure              # プロジェクト健全性スコア（0-100）
```

## デモ

### 再現可能なE2Eデモ — 3つの伝搬パターン

以下のデモはコミット [`d7d9f45`](https://github.com/yohey-w/codd-dev/commit/d7d9f45) に固定されている。ローカルで完全再現可能。

**セットアップ:**
```bash
pip install codd-dev>=1.6.0
mkdir demo && cd demo && git init
cat > spec.txt << 'EOF'
TaskFlow — Requirements
- User authentication (email + Google OAuth)
- Workspace management (teams, roles, invites)
- Task CRUD with assignees, labels, due dates
- Real-time updates (WebSocket)
- File attachments (S3)
- Notification system (in-app + email)
EOF
codd init --project-name "taskflow" --language "typescript" --requirements spec.txt
```

**パターン1 — Source → Doc**（仕様 → 設計書）:
```bash
codd plan --init
for wave in $(seq 1 $(codd plan --waves)); do codd generate --wave $wave; done
codd validate        # 期待値: PASS, エラー0件
codd scan            # 期待値: 17ノード, 30+エッジ
```

**パターン2 — Doc → Doc**（要件変更 → 下流更新）:
```bash
# 要件を編集: 認証に「SSO (SAML 2.0)」を追加
codd impact          # 期待値: 7本中6本がGreen/Amberバンド
codd propagate --update  # 下流設計書を自動更新
```

**パターン3 — Doc → Doc via CEG**（コード変更 → 設計書更新）:
```bash
# authモジュールのソースコードを変更
codd propagate       # 期待値: auth-design, system-designが影響あり
codd propagate --update  # コードdiffからAIが影響設計書を更新
```

**期待出力**: 20行のspec → 17設計成果物（5,100行超） → 変更後もpropagateで全文書の一貫性を維持。パターン3（CEGベースの伝搬）は新規性が高い — コード変更を依存グラフ経由で設計書まで遡って更新するツールは他にない。

### グリーンフィールド — specから動くアプリまで

37行のspec → 設計書6本（1,353行） → コード102ファイル（6,445行） → TypeScript strictビルド成功。AIチャットなし — ワークフロー全体がシェルスクリプト。

詳細: [Harness as Code — CoDD活用ガイド #1](https://zenn.dev/shio_shoppaize/articles/codd-greenfield-guide)

### ブラウンフィールド — 変更影響分析

要件を2行変えただけで、`codd impact` が7本中6本の設計書に影響ありと判定。Green帯域はAIが自動更新。Amber帯域は人間に提示。変更が怖くなくなる。

詳細: [CoDD 詳細解説](https://zenn.dev/shio_shoppaize/articles/shogun-codd-coherence)

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
  modules: ["api", "auth"]        # ← ソースコードモジュールとの紐付け
  depends_on:
    - id: "design:system-design"
      relation: derives_from
    - id: "req:my-project-requirements"
      relation: implements
---
```

`modules` フィールドが逆方向トレーサビリティを実現する：ソースコードが変更されたとき、`codd extract` が影響モジュールを特定し、`modules` フィールドでそのモジュールに紐づく設計書を逆引きできる。

`codd/scan/` はキャッシュ — `codd scan` のたびに再生成されます。

## AIモデル設定

CoDDは設計書生成に外部AI CLIを呼び出す。デフォルトはClaude Opus：

```yaml
# codd.yaml
ai_command: "claude --print --model claude-opus-4-6"
```

### コマンド別オーバーライド

コマンドごとに異なるモデルを使い分けられる。例えば、設計書生成はOpus、コード実装はCodex：

```yaml
ai_command: "claude --print --model claude-opus-4-6"   # グローバルデフォルト
ai_commands:
  generate: "claude --print --model claude-opus-4-6"    # 設計書生成
  restore: "claude --print --model claude-opus-4-6"     # ブラウンフィールド復元
  review: "claude --print --model claude-opus-4-6"      # 品質評価
  plan_init: "claude --print --model claude-sonnet-4-6" # wave_config計画
  implement: "codex --print"                             # コード生成
```

**優先順位**: CLI `--ai-cmd` フラグ > `ai_commands.{コマンド}` > `ai_command` > ビルトインデフォルト（Opus）。

### Claude Codeのコンテキスト干渉

`claude --print` をプロジェクトディレクトリ内で実行すると、`CLAUDE.md` を自動検出してプロジェクトレベルのシステムプロンプトを読み込む。これらの指示がCoDDの生成プロンプトと競合し、フォーマットバリデーション失敗を起こすことがある：

```
Error: AI command returned unstructured summary for 'ADR: ...'; missing section headings
```

**対策**: `--system-prompt` でプロジェクトコンテキストを上書きし、文書生成に集中させる：

```yaml
ai_command: "claude --print --model claude-opus-4-6 --system-prompt 'You are a technical document generator. Output only the requested Markdown document. Follow section heading instructions exactly.'"
```

> **注意**: `--bare` は全コンテキストを排除するが、OAuth認証まで無効化してしまう。`--system-prompt` を使えば `CLAUDE.md` を上書きしつつ認証は維持できる。

## 設定ディレクトリの自動検出

デフォルトでは `codd init` は `codd/` ディレクトリを作成する。プロジェクトに既に `codd/` が存在する場合（例：ソースコードのパッケージ名）、`--config-dir` で別名を指定できる：

```bash
codd init --config-dir .codd --project-name "my-project" --language "python"
```

`scan`、`impact`、`generate` 等の全コマンドは、`codd/` → `.codd/` の順で設定ディレクトリを自動検出する。追加フラグは不要。

## ブラウンフィールド？ ここから

既存コードベースがある場合、CoDDはコード抽出から設計書復元までの完全なブラウンフィールドワークフローを提供する。

ウォークスルー: [Harness as Code — CoDD実践ガイド #2 ブラウンフィールド編](https://zenn.dev/shio_shoppaize/articles/shogun-codd-brownfield)

### AI抽出（--ai）

> **プリセットについて**: `codd extract --ai` には**baseline**プリセット（公開用）が同梱されている。公開ベンチマーク（F1 0.953+）の数値は、チューニング済みプリセットと内部評価セットで測定した結果であり、公開版baselineとは異なる。baselineは同じワークフローと出力形式を使えるが、結果はコードベースやプロンプトにより変動する。`--prompt-file` で独自のプロンプトを渡すことも可能。

```bash
codd extract --ai                        # 組み込みbaselineプリセットを使用
codd extract --ai --prompt-file my.md    # カスタムプロンプトを使用
```

### Step 1: コードから構造を抽出

`codd extract` がソースコードから設計書を逆生成する。AI不要——純粋な静的解析。

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

### Step 2: 抽出結果からwave_configを生成

`codd plan --init` は抽出済み設計書を自動検出し、要件定義なしでwave_configを生成する。

```bash
codd plan --init    # codd/extracted/ を検出し、ブラウンフィールド用wave_configを生成
```

生成されたwave_configの各成果物には `modules` フィールドが含まれ、ソースコードモジュールとの逆方向トレーサビリティを実現する。

### Step 3: 設計書を復元

`codd restore` は抽出された事実から設計書を復元する。`codd generate`（要件から設計書を生成）とは根本的に異なり、「現在の設計は何か？」をコード構造から再構築する。

```bash
codd restore --wave 2   # 抽出事実からシステム設計を復元
codd restore --wave 3   # DB設計・API設計を復元
```

### Step 4: 依存グラフを構築

```bash
codd scan
codd impact
```

**設計思想**: V-Modelにおいて、意図は要件定義にのみ存在する。アーキテクチャ・詳細設計・テストは構造事実——コードから抽出可能。`codd extract` は構造を取り、`codd restore` は設計を復元し、「なぜ」は後から人が加える。

### グリーンフィールド vs ブラウンフィールド

| | グリーンフィールド | ブラウンフィールド |
|--|-----------|-----------|
| 起点 | 要件定義（人間が記述） | 既存コードベース |
| 計画 | `codd plan --init`（要件から） | `codd plan --init`（抽出結果から） |
| 設計書生成 | `codd generate`（順方向: 要件→設計） | `codd restore`（逆方向: コード事実→設計） |
| トレーサビリティ | `modules` フィールドが設計書→コードを接続 | 同じ |
| 変更時 | `codd propagate`（コード→影響設計書→AI更新） | 同じ |

## コマンド

| コマンド | ステータス | 説明 |
|---------|--------|-------------|
| `codd init` | **安定版** | プロジェクト初期化（`--config-dir .codd` で設定ディレクトリ名を変更可） |
| `codd scan` | **安定版** | フロントマターから依存グラフ構築 |
| `codd impact` | **安定版** | 変更影響分析（Green / Amber / Gray バンド） |
| `codd validate` | **アルファ** | フロントマター整合性 & グラフ一貫性チェック |
| `codd generate` | 実験的 | Wave順で設計書生成（グリーンフィールド） |
| `codd restore` | 実験的 | 抽出事実から設計書を復元（ブラウンフィールド） |
| `codd plan` | 実験的 | Wave実行状況（`--init` はブラウンフィールドにも対応） |
| `codd verify` | 実験的 (Pro) | V-Model検証 |
| `codd implement` | 実験的 | 設計書→コード生成 |
| `codd propagate` | **アルファ** | コード/設計変更を下流の影響設計書に伝搬 |
| `codd review` | 実験的 (Pro) | AI品質評価（LLM-as-Judge） |
| `codd extract` | **アルファ** | 既存コードから設計書を逆生成 |
| `codd require` | **アルファ** | 既存コードベースから要件を推論（ブラウンフィールド） |
| `codd audit` | **アルファ** (Pro) | 変更レビュー一括パック（validate + impact + policy + review） |
| `codd policy` | **アルファ** | エンタープライズポリシーチェッカー（ソースコードの禁止/必須パターン） |
| `codd measure` | **アルファ** | プロジェクト健全性メトリクス（グラフ、カバレッジ、品質、スコア 0-100） |
| `codd mcp-server` | **アルファ** | AIツール連携用MCPサーバー（stdio、依存ゼロ） |

## OSS / Pro 分離

CoDD v1.6.0でOSS/Proの境界をブリッジパターンで明確化。

**OSS（MIT、無料）** — 文書の一貫性維持に必要な全機能:

`init` · `scan` · `impact` · `generate` · `restore` · `propagate` · `extract` · `require` · `plan` · `validate` · `measure` · `policy` · `mcp-server`

**Pro（プライベート、有料）** — エンタープライズ向けレビュー・検証:

`review` · `verify` · `audit` · `risk`

```bash
# OSSのみ
pip install codd-dev

# Pro拡張を追加
pip install "codd-pro @ git+ssh://git@github.com/yohey-w/codd-pro.git"
```

`codd-pro` がインストールされていれば、entry-pointsプラグイン探索でPro実装がOSSフォールバックを自動的にオーバーライドする。未インストール時はマイグレーションメッセージを表示して正常終了。設定変更は不要。

## CI連携（GitHub Action）

プルリクエストごとにCoDD監査を実行。判定結果（APPROVE / CONDITIONAL / REJECT）、バリデーション結果、ポリシー違反、影響分析をコメントとして投稿する。

### クイックセットアップ

プロジェクトに `.github/workflows/codd.yml` を追加:

```yaml
name: CoDD Audit
on:
  pull_request:
    branches: [main]

permissions:
  contents: read
  pull-requests: write

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: yohey-w/codd-dev@main
        with:
          diff-target: origin/${{ github.base_ref }}
          skip-review: "true"  # AIレビューを有効にするには "false" に設定
```

### Action入力

| 入力 | デフォルト | 説明 |
|------|-----------|------|
| `diff-target` | `origin/main` | 差分比較対象のGit ref |
| `skip-review` | `true` | AIレビューフェーズをスキップ（高速、AIコスト不要） |
| `python-version` | `3.12` | Pythonバージョン |
| `codd-version` | 最新版 | 特定バージョン（例: `>=1.3.0`） |
| `post-comment` | `true` | 結果をPRコメントとして投稿 |

### Action出力

| 出力 | 説明 |
|------|------|
| `verdict` | `APPROVE`、`CONDITIONAL`、または `REJECT` |
| `risk-level` | `LOW`、`MEDIUM`、または `HIGH` |
| `report-json` | JSON監査レポートへのパス |

### エンタープライズポリシー

`codd.yaml` でソースコードポリシーを定義:

```yaml
policies:
  - id: SEC-001
    description: "ハードコードされたパスワードの禁止"
    severity: CRITICAL
    kind: forbidden
    pattern: 'password\s*=\s*[''"]'
    glob: "*.py"

  - id: LOG-001
    description: "全モジュールにloggingのimportが必要"
    severity: WARNING
    kind: required
    pattern: "import logging"
    glob: "*.py"
```

ポリシーチェッカーは `codd audit` の一部として実行され、`codd policy` で単独実行も可能。CRITICAL違反はREJECT、WARNINGはCONDITIONALを返す。

## MCPサーバー

CoDDは [Model Context Protocol](https://modelcontextprotocol.io/) 経由でツールを公開し、AIツールとの直接連携を実現する。外部依存ゼロ — MCP対応クライアントならどれでも動作。

```bash
codd mcp-server --project /path/to/your/project
```

### Claude Code設定

`~/.claude/claude_code_config.json` に追加:

```json
{
  "mcpServers": {
    "codd": {
      "command": "codd",
      "args": ["mcp-server", "--project", "/path/to/your/project"]
    }
  }
}
```

### 利用可能なMCPツール

| ツール | 説明 |
|--------|------|
| `codd_validate` | フロントマター整合性とグラフ一貫性チェック |
| `codd_impact` | 指定ノードまたはファイルの変更影響分析 |
| `codd_policy` | エンタープライズポリシールールに対するソースコードチェック |
| `codd_audit` | 変更レビュー一括実行（validate + impact + policy） |
| `codd_scan` | 設計書から依存グラフ構築 |
| `codd_measure` | プロジェクト健全性メトリクス（グラフ、カバレッジ、品質、スコア） |

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

殿:  （ソースコードを変更 — SSO機能を実装）

殿:  /codd-propagate
     → Claude: codd propagate --path .
     → 「authモジュールで3ファイル変更。影響を受ける設計書2件:
        design:system-design, design:auth-detail」
     → 「--updateで設計書を更新しますか？」

殿:  はい
     → Claude: codd propagate --path . --update
     → 更新された設計書をレビューし、変更内容が正確か確認
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
| `/codd-generate` | HITLゲート付きWave順設計書生成（グリーンフィールド） |
| `/codd-restore` | 抽出事実から設計書復元（ブラウンフィールド） |
| `/codd-scan` | 依存グラフ再構築 |
| `/codd-impact` | Green/Amber/Grayプロトコルで変更影響分析 |
| `/codd-validate` | フロントマター & 依存関係の整合性チェック |
| `/codd-propagate` | ソースコード変更を設計書に逆伝搬 |
| `/codd-review` | AI品質レビュー（PASS/FAIL判定 + フィードバック） |

詳細は [docs/claude-code-setup_ja.md](docs/claude-code-setup_ja.md) を参照。

## 自律品質ループ

`codd review` はAI（LLM-as-Judge）で成果物を評価し、`--feedback` はその結果を再生成に食わせる。組み合わせることで完全自律の品質ループが回る：

```bash
# 生成 → レビュー → フィードバック付き再生成 → PASSするまで繰り返し
codd generate --wave 2 --force
feedback=$(codd review --path . --json | jq -r '.results[0].feedback')
verdict=$(codd review --path . --json | jq -r '.results[0].verdict')

while [ "$verdict" = "FAIL" ]; do
  codd generate --wave 2 --force --feedback "$feedback"
  result=$(codd review --path . --json)
  verdict=$(echo "$result" | jq -r '.results[0].verdict')
  feedback=$(echo "$result" | jq -r '.results[0].feedback')
done
```

レビュー基準はドキュメントタイプ別：

| タイプ | 評価基準 |
|--------|----------|
| 要件定義 | 網羅性、一貫性、テスト可能性、曖昧さ |
| 設計 | アーキテクチャ妥当性、API品質、セキュリティ、上流整合性 |
| 詳細設計 | 実装明確性、データモデル、エラー処理、インターフェース契約 |
| テスト | カバレッジ、エッジケース、独立性、トレーサビリティ |

**スコアリング**: 80点以上 = PASS。CRITICALイシューは自動で59点に上限。FAIL時はexit code 1 — ループ親和。

**モデル配置**: レビューはOpus（`ai_commands.review`）、実装はCodex（`ai_commands.implement`）。`ai_commands` 設定で1行で切り替え。

## 他のSpec駆動ツールとの違い

主要なSpec駆動ツールはすべて設計書の**作成**に焦点を当てている。設計書が**変更された後**に何が起きるかに対処するツールはない。CoDDは依存グラフ、影響分析、バンドベースの更新プロトコルでそのギャップを埋める。

| | **spec-kit** (GitHub) | **Kiro** (AWS) | **cc-sdd** (gotalab) | **CoDD** |
|--|---|---|---|---|
| 焦点 | Spec作成（要件→設計→タスク→コード） | Agentic IDE + SDD | Kiro式SDDのClaude Code版 | **作成後の一貫性維持** |
| Stars | 83.7k | N/A（プロプラIDE） | 3k | -- |
| 変更伝播 | No | No | No | **`codd impact` + 依存グラフ** |
| 影響分析 | No | No | No | **Green / Amber / Gray バンド** |
| 仕様記法 | Markdown + 40拡張 | EARS記法 | 品質ゲート + git worktree | フロントマター `depends_on` |
| ハーネスロックイン | GitHub Copilot | Kiro IDE | Claude Code | **任意のエージェント / IDE** |

要約: spec-kit、Kiro、cc-sddは「仕様をどう作るか」に答える。CoDDは「上流が変わったとき、下流を自動で更新する」に答える。

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

### CoDD自身の開発もCoDDで管理

CoDDは自分自身をdogfoodingしている。`.codd/`ディレクトリにCoDD自身の設定があり、`codd extract`で自分のソースコードから設計書を逆生成する。V-Modelライフサイクル全体が自分自身に対して動く：

```bash
codd init --config-dir .codd --project-name "codd-dev" --language "python"
codd extract          # 15モジュール → 依存フロントマター付き設計書
codd scan             # 49ノード、83エッジ
codd verify           # mypy + pytest（434テスト通過）
```

自分自身を管理できないツールに、あなたのプロジェクトは任せられない。

## ロードマップ

- [ ] セマンティック依存関係タイプ (`requires`, `affects`, `verifies`, `implements`)
- [x] `codd extract` — 既存コードベースから設計書を逆生成（ブラウンフィールド対応）
- [x] `codd restore` — 抽出事実から設計書を復元（ブラウンフィールド設計書生成）
- [x] `codd plan --init` ブラウンフィールド対応 — 抽出結果からwave_config生成
- [x] `modules` フィールド — 設計書 ↔ ソースコードのトレーサビリティ
- [x] コマンド別AIモデル設定（`ai_commands` in codd.yaml）
- [x] `codd propagate` — ソースコード変更を設計書に逆伝搬
- [x] `codd review` — AI品質評価 + レビュー駆動の再生成ループ
- [x] `--feedback` フラグ — レビュー結果をgenerate/restore/propagateに食わせて再生成
- [x] `codd verify` — 言語非依存の検証（Python: mypy + pytest、TypeScript: tsc + jest）
- [x] `codd require` — 既存コードベースから要件を推論（確信度タグ付き）
- [x] `codd audit` — 変更レビュー一括パック（validate + impact + policy + review）
- [x] `codd policy` — エンタープライズポリシーチェッカー（禁止/必須パターン）
- [x] `codd measure` — プロジェクト健全性メトリクス（グラフ、カバレッジ、品質、スコア 0-100）
- [x] GitHub Action — PRの自動監査コメント付きCI連携
- [x] MCPサーバー — AIツール連携用stdio JSON-RPCサーバー
- [x] プラグインシステム — 拡張可能なrequireプロンプト（タグ、エビデンス形式、出力セクション）
- [ ] マルチハーネス連携例（Claude Code, Copilot, Cursor）
- [ ] VS Code拡張（影響分析の可視化）

## 解説記事

- [Zenn: Harness as Code — CoDD活用ガイド #1 spec → 設計書 → コード](https://zenn.dev/shio_shoppaize/articles/codd-greenfield-guide)
- [Zenn: Harness as Code — CoDD実践ガイド #2 ブラウンフィールド編](https://zenn.dev/shio_shoppaize/articles/shogun-codd-brownfield)
- [Zenn: Harness as Code — CoDD活用ガイド #3 既存コードのバグ修正（SWE-bench実験）](https://zenn.dev/shio_shoppaize/articles/codd-swebench-pilot)
- [Zenn: CoDD 詳細解説](https://zenn.dev/shio_shoppaize/articles/shogun-codd-coherence)
- [dev.to: Harness as Code — Treating AI Workflows Like Infrastructure](https://dev.to/yohey-w/harness-as-code-treating-ai-workflows-like-infrastructure-27ni)
- [dev.to: What Happens After "Spec First"](https://dev.to/yohey-w/codd-coherence-driven-development-what-happens-after-spec-first-514f)

## ライセンス

MIT
