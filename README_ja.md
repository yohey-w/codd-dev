<p align="center">
  <strong>CoDD — Coherence-Driven Development</strong>
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

## 🚀 60秒で始める

```bash
pip install codd-dev

# プロジェクトのルートで
codd init --suggest-lexicons --llm-enhanced   # AI が必要な lexicon を選定
codd elicit                                    # AI が要件の穴を発見
codd dag verify --auto-repair --max-attempts 10  # AI が整合性違反を自動修復
```

これだけ。3 コマンド、3 つのフィードバックループ、1 つの一貫したプロジェクト。

> 実プロジェクトで実証済: Next.js + Prisma + PostgreSQL の LMS で dogfooding。詳細は [ケーススタディ](#-ケーススタディ-実プロジェクト-lms)。

---

## ✨ できること

| コマンド | 一行説明 |
| --- | --- |
| 🔍 **`codd elicit`** | LLM が要件の **仕様の穴** を発見。業界標準 lexicon (BABOK / OWASP / WCAG / PCI DSS / ISO 25010 等) でスコープ。 |
| 🔄 **`codd diff`** | 要件と実装の **drift** を検出 (brownfield 対応)。 |
| 🛠️ **`codd dag verify --auto-repair`** | 要件→設計→実装→テストの DAG を検証、違反があれば LLM が patch 提案、ループで SUCCESS or MAX_ATTEMPTS まで再試行。 |
| 📦 **38 lexicon プラグイン** | 業界標準を opt-in 同梱: Web (WCAG / OWASP / Web Vitals / WebAuthn / forms / SEO / PWA / browser-compat / responsive)、Mobile (HIG / Material 3 / a11y / MASVS)、Backend (REST / GraphQL / gRPC / events)、Data (SQL / JSON Schema / event sourcing / governance)、Ops (CI/CD / Kubernetes / Terraform / observability / DORA)、Compliance (ISO 27001 / HIPAA / PCI DSS / GDPR / EU AI Act)、Process (ISO 25010 / 29119 / DDD / 12-factor / i18n / model cards / API rate-limit)、Methodology (BABOK)。 |
| 🌐 **`codd brownfield`** | extract → diff → elicit パイプライン: 既存コードベースに向けると要件を逆抽出して drift と仕様穴を一発で出す。 |
| 🎯 **`codd init --suggest-lexicons --llm-enhanced`** | LLM がコード/ドキュメントを読み、データ種別と機能特性を抽出して lexicon を推奨 (信頼度 + 理由付き)。 |
| 📊 **`codd lexicon list/install/diff` + `codd coverage report`** | プラグイン管理 + JSON / Markdown / 自己完結 HTML のカバレッジマトリクス出力。 |
| 🛡️ CI ゲート | `.github/workflows/codd_coverage.yml` テンプレ + `codd coverage check` の exit code でカバレッジ後退を merge ブロック。 |

---

## 🎨 ビジュアルフロー

```mermaid
flowchart LR
    R["要件 (.md)"] --> E["codd elicit"]
    E -->|gap findings| H{HITL: approve / reject}
    H -->|[x]| L["project_lexicon.yaml + 要件 TODO"]
    H -->|[r]| I["ignored_findings.yaml"]
    L --> V["codd dag verify --auto-repair"]
    V -->|違反| AR["LLM patch 提案 → 適用"]
    AR --> V
    V -->|SUCCESS| D["✅ deploy ゲート PASS"]
    AR -->|max attempts| P["PARTIAL_SUCCESS: unrepairable を正直に提示"]
```

Brownfield (既存コード起点) パス:

```mermaid
flowchart LR
    Code["既存コードベース"] --> X["codd extract"]
    X --> DIFF["codd diff (drift)"]
    DIFF --> EL["codd elicit (coverage gaps)"]
    EL --> H{HITL ゲート}
    H --> Apply["codd elicit apply"]
    Apply --> V["codd dag verify"]
```

---

## 📊 ケーススタディ: 実プロジェクト LMS

Next.js + Prisma + PostgreSQL のマルチテナント LMS (設計書約30本、DB 12テーブル、RLS で完全分離):

| ステージ | 結果 |
| --- | --- |
| `codd init --suggest-lexicons --llm-enhanced` | LLM が **データ種別** (個人情報 / 決済 / 動画) と **機能特性** (認証 / 決済 / public REST) を検出、15 lexicon を推奨 → 殿選定 10 のうち 9 と一致、ヒューリスティクスを実証。 |
| `codd elicit` (10 lexicon ロード、scope=`system_implementation`、phase=`mvp`) | **70 findings** (web a11y / data governance / SQL / security / Web Vitals / WebAuthn / API / process)。業務系 (KPI / UAT 詳細 / リスク登録) は scope filter で自動除外。 |
| `codd dag verify --auto-repair` | 当初 unrepairable=16 → core 改善 (deployment chain auto-discover、runtime-state auto-bind、mock harness no-op、scope/phase filter) を経て **PASS or amber-WARN (deploy 許可)** に到達。 |
| VPS smoke (`/`, `/login`, `/api/health`) | 3 エンドポイント全て **200 OK**。 |

パイプライン全体の改修において、**プロジェクト個別の修正は CoDD core に 0 行** — プロジェクト固有の関心事は全て `project_lexicon.yaml` か `codd_plugins/` (Generality Gate、Layer A/B/C) に閉じる。

---

## 🌟 なぜ CoDD が存在するのか

> **「機能要件と制約だけ書けば、コードは自動生成・自動修復・自動検証される」**

多くの「AI 支援開発」ツールは **生成側** に焦点を当てる。CoDD は **制約側** に焦点を当てる: LLM は「何が真でなければならないか」が明確なときに最も役に立つ。CoDD はその明確な像を、全成果物を結ぶ DAG として与え、業界標準 (BABOK / WCAG / OWASP / PCI / ISO) を制約として機械的に供給するプラグイン面を提供する。

DAG が壊れると LLM が patch を提案、ループが再検証、最終的に SUCCESS に到達するか、構造的に修復不可能なものを正直に提示する。

### Generality Gate (三層アーキテクチャ)

| Layer | スタック固有名がある場所 | 例 |
| --- | --- | --- |
| **A — Core** | **どこにもない。** `react`, `django`, `Stripe`, `LMS` 等 0 hardcode。 | `codd/elicit/`, `codd/dag/`, `codd/lexicon_cli/` |
| **B — Templates** | 汎用プレースホルダーのみ。 | `codd/templates/*.j2`, `codd/templates/lexicon_schema.yaml` |
| **C — Plug-ins** | 何でも自由に命名 OK。 | `codd_plugins/lexicons/*/`, `codd_plugins/stack_map.yaml` |

これにより、Next.js / Django / FastAPI / Rails / Go service / モバイル / ML モデルカードに対し **同じ core が動く**、かつ contributor は core を触らずに lexicon を追加できる。

---

## 🧭 Roadmap (v2.x → v3.0)

- **v2.x (現在)** — Lexicon-driven completeness、38 plug-in、LLM 強化 init、scope/phase filter、DAG 全体での auto-repair。
- **v3.0 (予定)** — Sprint 廃止 implement パイプライン (設計書 → ImplStep → コード、人間プロジェクト管理層を AI フローから除去)。

---

## 🤝 貢献者

CoDD は以下の方々によって形作られている:

- **[@yohey-w](https://github.com/yohey-w)** — Maintainer / Architect
- **[@Seika86](https://github.com/Seika86)** — Sprint regex 知見 (PR #11)
- **[@v-kato](https://github.com/v-kato)** — brownfield 再現報告 (Issue #17 / #18 / #19)
- **[@dev-komenzar](https://github.com/dev-komenzar)** — `source_dirs` バグ再現 (Issue #13)

外部からの issue / PR / lexicon 提案を歓迎する — [Issues](https://github.com/yohey-w/codd-dev/issues) 参照。

---

## 📚 ドキュメント

- [CHANGELOG.md](CHANGELOG.md) — 各 release の品質メトリクス
- [docs/](docs/) — アーキテクチャノート
- `codd --help` — CLI 全リファレンス

---

## 📦 Hook integration

CoDD は editor / Git ワークフロー用の hook recipe を同梱:

- Claude Code `PostToolUse` hook recipe — ファイル編集後に CoDD チェック実行
- Git `pre-commit` hook recipe — coherence check 違反時にコミットブロック

Recipes は `codd/hooks/recipes/` にある。

---

## ライセンス

MIT — [LICENSE](LICENSE) 参照。

## リンク

- [PyPI](https://pypi.org/project/codd-dev/)
- [GitHub Sponsors](https://github.com/sponsors/yohey-w) — 開発支援
- [Issues](https://github.com/yohey-w/codd-dev/issues)

---

> 「コードが変わったとき、CoDD は影響範囲を追跡し、違反を検出し、マージ判断のためのエビデンスを生成する。」
