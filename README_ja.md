<p align="center">
  <strong>CoDD — Coherence-Driven Development（一貫性駆動開発）</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/v/codd-dev?style=flat-square&color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/pyversions/codd-dev?style=flat-square" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License"></a>
  <a href="https://github.com/yohey-w/codd-dev/stargazers"><img src="https://img.shields.io/github/stars/yohey-w/codd-dev?style=flat-square" alt="Stars"></a>
</p>

<p align="center">
  日本語 | <a href="README.md">English</a> | <a href="README_zh.md">中文</a>
</p>

---

## 機能要件と制約だけ書けば、コードは自動で書ける。

CoDD は **要件 → 設計 → 実装 → テスト** をひとつの DAG として扱い、各ノードの一貫性 (coherence) を機械検証して、不整合があれば LLM が自動で修復する開発エンジンである。

人間が書くのは **「何を」と「どこまで」** だけ。「どう書くか」は CoDD と LLM が引き受ける。

```bash
pip install codd-dev
```

---

## Quick Start (5 分)

### 1. インストール

```bash
pip install codd-dev
codd --version  # 1.34.0 以上
```

### 2. プロジェクトに codd.yaml を置く

```yaml
# codd.yaml
codd_required_version: ">=1.34.0"

dag:
  design_docs:
    - "docs/design/**/*.md"
  implementations:
    - "src/**/*.{ts,tsx,py}"
  tests:
    - "tests/**/*.{spec,test}.{ts,tsx,py}"

repair:
  approval_mode: required   # 自動修復には人の承認を要する
  max_attempts: 10

llm:
  ai_command: "claude"      # 任意の LLM CLI を呼び出せる (claude / codex / gemini 等)
```

### 3. 典型コマンド

```bash
# 整合性検証 (要件・設計・実装・テストの一貫性チェック)
codd dag verify

# 自動修復付き検証 (違反を見つけたら LLM が patch を生成・適用)
codd dag verify --auto-repair --max-attempts 10

# User Journey の実機 PASS 確認 (CDP 経由でブラウザ操作)
codd dag run-journey login_to_dashboard --axis viewport=smartphone_se

# 設計書から実装手順を導出 (実装段階の入力)
codd implement run --task M1.2 --enable-typecheck-loop
```

### 4. 出力の見方

`codd dag verify` は 9 種の coherence check を走らせる:

| Check | 役割 |
|-------|------|
| `node_completeness` | 設計書記載のノード (実装/テスト) が物理ファイルとして存在するか |
| `transitive_closure` | 要件 → 設計 → 実装 → テストの依存連鎖が閉じているか |
| `verification_test_runtime` | 実装に対するテストが実行可能で PASS するか |
| `deployment_completeness` | デプロイチェーン (Dockerfile/compose/k8s) が完備か |
| `proof_break_authority` | 重要 journey が壊れていないか |
| `screen_flow_edges` | 画面遷移グラフに孤立ノードがないか |
| `screen_flow_completeness` | 全画面が要件にマップされているか |
| `c8` | uncommitted patch / dirty file の検知 |
| `c9` (`environment_coverage`) | viewport / RBAC role / locale 等の **対象環境網羅性** |

violation が見つかれば deploy gate を block、`--auto-repair` で LLM patch 生成 → 適用 → 再検証のループに入る。

---

## 典型ユースケース

### ユースケース 1: 要件 → 設計 → 実装の自動化

`docs/requirements/*.md` に「機能要件 + 制約」を書き、`codd implement run` を呼ぶと:

1. 要件から ImplStep 列を LLM が動的導出 (Layer 1)
2. ベストプラクティス補完 (Layer 2、ログイン → ログアウト/Remember Me/セッションタイムアウト 等)
3. ユーザー承認 (HITL gate) を経て `src/**` に実装が生成される
4. 生成中に `tsc` などの type check が落ちれば自動修復ループに入る

人間は「機能要件 + 制約だけ書けば全自動」を体験できる。

### ユースケース 2: Auto-Repair (codd verify --auto-repair)

CI で `codd dag verify --auto-repair --max-attempts 10` を回すと:

1. 9 種の coherence check が実行される
2. 違反を **修復可能 (in-task) / pre-existing (baseline) / unrepairable** に Hybrid Classifier (git diff + LLM) で分類
3. 修復可能違反のうち DAG 上最も上流のものを選んで LLM が patch 生成
4. dry-run validation を経て apply、再検証
5. max_attempts 内で全解消 → `SUCCESS`、一部修復 → `PARTIAL_SUCCESS`、修復不能ばかり → `REPAIR_FAILED`

`PARTIAL_SUCCESS` でも修復済 patch は反映され、残違反は report に列挙される (透明性)。

### ユースケース 3: User Journey Coherence (codd dag run-journey)

`docs/design/auth_design.md` の frontmatter にユーザージャーニーを書く:

```yaml
user_journeys:
  - name: login_to_dashboard
    criticality: critical
    steps:
      - { action: navigate, target: "/login" }
      - { action: fill, selector: "input[type=email]", value: "user@example.com" }
      - { action: click, selector: "button[type=submit]" }
      - { action: expect_url, value: "/dashboard" }
```

`codd dag run-journey login_to_dashboard --axis viewport=smartphone_se` で:

- `project_lexicon.yaml` 宣言の `viewport=smartphone_se` (375x667) を CDP に runtime 注入
- 実機ブラウザ (Edge / Chrome) で journey を実行
- 失敗時は `codd dag verify` の C9 environment_coverage が deploy gate を block

スマホ専用 nav が消えてた等の事故を構造的に防ぐ。

---

## v1.34.0 主要機能

| 機能 | 役割 |
|------|------|
| **DAG 完全性** (C1〜C8) | 要件・設計・実装・テスト・デプロイの 9 種コヒーレンスチェック |
| **Coverage Axis Layer** (C9) | viewport / RBAC role / locale 等の **対象環境網羅性** を統一抽象 (16+ 軸対応) で検証 |
| **LLM Auto-Repair (RepairLoop)** | 違反検知 → LLM patch 生成 → apply → 再検証のループ、`max_attempts` 内で全解消を試行 |
| **Hybrid Classifier** | git diff (Stage 1) + LLM 判断 (Stage 2) で violation を repairable / pre_existing / unrepairable に分類 |
| **Primary Picker** | 複数違反のうち DAG 上最も上流のもの (root cause 候補) を優先修復 |
| **PARTIAL_SUCCESS policy** | applied_patches OR pre_existing OR unrepairable があれば PARTIAL_SUCCESS、CI を release blocker から外せる |
| **BestPracticeAugmenter** | 設計書に明記されないベストプラクティス (パスワードリセット等) を LLM が動的補完 |
| **ImplStepDeriver (2-layer)** | 設計書 → ImplStep 列の動的展開、Layer 2 で `required_axes` 推論 |
| **Typecheck Repair Loop** | 実装段階で `tsc --noEmit` などの type check が落ちたら自動修復ループ |
| **`codd version --check --strict`** | プロジェクト要求 vs インストール済 codd の差分検出 |

詳細は [CHANGELOG.md](CHANGELOG.md) 参照。

---

## 実証ケーススタディ — 実プロジェクト (LMS Web App)

実プロジェクト (LMS アプリ、Next.js + Prisma + PostgreSQL) で `codd verify --auto-repair --max-attempts 10` を実行した結果:

```
status:                PARTIAL_SUCCESS
attempts:              4
applied_patches:       4
pre_existing_violations:  1
unrepairable_violations:  2
remaining_violations:     3 (skip + report 済み)
smoke proof:           6 checks PASS
CoDD core 改修:        0 行
```

修復された file:
- `tests/e2e/environment-coverage.spec.ts`
- `tests/e2e/login.spec.ts`

スキップされた違反 (CoDD 責任外として report に明示):
- pre_existing: deployment_completeness chain
- unrepairable: Dockerfile dry-run patch validation
- unrepairable: Vitest matcher runtime issue

C9 environment_coverage は viewport (smartphone_se / desktop_1920) と RBAC role (central_admin / tenant_admin / learner) の axis × variant 全網羅を検証、PASS 達成。

---

## アーキテクチャ — 4 release 進化

| Release | 到達点 |
|---------|--------|
| v1.31.0 | 内側 100% (内部整合性 coherence) — type check repair loop で「手動 type fix」を撲滅 |
| v1.32.0 | 外側 100% (対象環境網羅性 Coverage Axis) — viewport/RBAC/locale 等を統一抽象で吸収 |
| v1.33.0 | caveats 解消経路実証 — 実機 CDP run-journey + LLM auto-repair attempt PASS |
| **v1.34.0** | **full pipeline 完全実証** — 実プロジェクトで auto-repair PARTIAL_SUCCESS 完走 |

詳細は [CHANGELOG.md](CHANGELOG.md) で各 release を参照。

---

## Generality Gate (汎用性絶対維持)

CoDD core code には以下の hardcode を **禁止** している:

- 特定 stack 名 (Next.js / Django / Rails / FastAPI 等)
- 特定 framework / library の literal
- 特定 domain (Web / Mobile / Desktop / CLI / Backend / Embedded)
- 特定 viewport 値 (375 / 1920 等) や device 名 (iPhone / Android 等)

これらは全て **`project_lexicon.yaml` (プロジェクト固有)** に閉じる。CoDD は generic な violation object としてのみ処理する。

LLM が「stack 固有の最適 patch」を提案する場合は、その判断は **LLM の知識** に委ね、CoDD core が決めない (= overfitting しない)。

---

## ライセンス

MIT License — [LICENSE](LICENSE) 参照。

## リンク

- [CHANGELOG.md](CHANGELOG.md) — 全 release ノート
- [GitHub Sponsors](https://github.com/sponsors/yohey-w) — 開発支援
- [Issues](https://github.com/yohey-w/codd-dev/issues) — バグ報告 / 機能要望

---

> 「コードが変わったとき、CoDD は影響範囲を追跡し、違反を検出し、マージ判断のためのエビデンスを生成する。」
