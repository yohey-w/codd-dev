---
name: codd-modify
description: |
  Modify the CoDD framework itself (codd-dev). Use when you need to add a new check, fix a bug in
  CoDD's core logic, change CoDD CLI behavior, or update verification logic. NOT for modifying
  applications built with CoDD — use codd-evolve for that. Handles all safety gates: anti-overfit,
  generality, versioning, dogfood verification, and Shogun approval for concept changes.
---

# CoDD Modify — Safe Framework Evolution

Modify `codd-dev` (the CoDD framework itself) while preserving generality, preventing overfitting,
and verifying changes against a real project (dogfood).

## Relationship to `codd-improve`

`codd-improve` (sibling skill) provides the **Generality Gate intuition** — the 5-question check with
real rejection cases from past incidents. **Read codd-improve first** for the conceptual foundation
of "why CoDD changes must stay generic." This skill (`codd-modify`) builds on top by adding:

- **Role gate** — who may invoke CoDD CLI commands
- **Versioning policy** — semver decisions, v3.0.0 reservation
- **Dogfood verification** — runtime smoke beyond pytest
- **Stop-and-Ask gates** — concept-change escalation to Shogun-tier
- **Forbidden patterns (F1-F10)** — explicit anti-patterns from cmd_338-343 incidents
- **Lessons Learned** — concrete bug records (L-1 to L-5) with reproduction context

## When to Use

- Fixing a bug in CoDD core logic (`codd/`, `codd/cli.py`, `codd/dag/`, `codd/llm/`, etc.)
- Adding a new check to `codd dag verify` or `codd verify --runtime`
- Changing CoDD CLI behavior (`codd implement`, `codd fix`, `codd plan`, etc.)
- Updating default values in CoDD config (timeout, retry, batch size, etc.)
- Adding a warning/diagnostic to `codd doctor`
- Extending SKILL.md of an existing CoDD skill

Do NOT use for:
- Modifying applications built with CoDD (osato-lms etc.) → use `/codd-evolve`
- Adding new CoDD concepts or node types → **requires Shogun judgment first**
- CoDD self-hosting (using CoDD to develop CoDD itself) → structurally impossible

---

## Who May Run This

**Role gate (absolute)**: Only karo / gunshi / shogun may invoke CoDD CLI commands.
Ashigaru (foot soldiers) must NOT run `codd verify`, `codd implement`, `codd dag`, or any
CoDD CLI command, regardless of their CLI tool (Claude/Codex/other).

This is a role-based rule, not a CLI-tool rule. It applies to any agent configuration.

---

## North Star — Internalize Before Touching Code

Read this before every modification. Do not skip.

**CoDD = Harness Engineering (理論的最終形)**
- Input: 要件定義 + 制約条件 (2 つだけ)
- Output: 設計書 + コード + テスト (全部)
- 変更時の整合性伝搬: 自動

**CoDD は domain agnostic**: Web / Mobile / CLI / ML / 組み込み — 全領域対応。
Core に domain 固有名 (テーブル名・画面名・フレームワーク名) を入れない。

**人間+AI 役割分担**:
- アプリ開発 (既存アーキ準拠) → AI 自律 OK
- CoDD 進化 (新概念創出 / 新 check / 新 node / 新 edge) → Shogun 判断必須

---

## Step 1: Entry Guard

必ず全部実行してから先へ進む。`<dogfood-project>` と `<codd-dev>` は環境ごとのパスに置換せよ
(例: `~/your-project` / `~/codd-dev`)。

```bash
# 1a. CoDD DAG 状態確認 (dogfood project — your real CoDD project)
cd <dogfood-project>
codd dag verify 2>&1 | tail -5
# → red=0 であること。red > 0 なら先に red を解消してから修正着手

# 1b. 現状テスト (codd-dev — this repository)
cd <codd-dev>
pytest --tb=short -q 2>&1 | tail -10
# → PASS / SKIP=0 であること (SKIP=FAIL ルール)

# 1c. codd-dev の git 状態確認
git status
git log --oneline -3
```

**Red が残っている場合**: `codd fix [PHENOMENON]` で先に red を解消すること。
Red 状態で codd fix を走らせると、DAG が壊れているため修正方針が嘘になる (drift 拡大)。

---

## Step 2: Change Classification

修正を以下に分類する。分類によって後続ステップが変わる。

| 分類 | 説明 | 例 |
|---|---|---|
| `bug_fix` | 既存機能の誤動作を正す | タイムアウト silent failure 修正、YAML parse エラー |
| `behavior_change` | 既存概念の範囲内で挙動を変える | default 値変更、出力形式変更、warning 追加 |
| `check_addition` | `codd dag verify` や `codd verify --runtime` に新 check を追加 | ui_coherence, node_completeness 追加 |
| `concept_change` | 新しい CoDD の概念・node・edge・アーキを導入 | operation_flow フィールド追加、新 DAG edge type |

→ `check_addition` または `concept_change` の場合は **Step 3 へ必ず進む**。
→ `bug_fix` / `behavior_change` のみ → Step 3 をスキップして Step 4 へ。

---

## Step 3: Stop-and-Ask Gate (concept_change / check_addition のみ)

**新 check / 新概念は Shogun の明示的承認が必要**。以下を inbox で確認を取ること。

確認すべき内容:
- この check/concept は CoDD の北極星「要件定義+制約 → 全自動」を前進させるか
- domain agnostic か (osato-lms 固有でないか)
- 汎用性チェック: 「Web 以外のプロジェクト (CLI / Mobile / ML) でも意味を成すか」
- 既存 check の延長か、全く新しい概念か

**Shogun が承認した場合のみ Step 4 へ進む**。

---

## Step 4: Anti-Overfit Gate (全変更種別必須)

全ての変更を実装する前に自問する。No が1つでも出たら設計を見直す。

### 4.1 汎用性チェック

```
□ この変更は osato-lms 以外のプロジェクトでも役立つか?
□ コード中にドメイン固有名が入っていないか?
  - NG例: "delivery_target", "admin/courses", "course_master", "tenant_admin"
  - NG例: 特定フレームワーク名 (Next.js, Django, Rails) を CoDD core に hardcode
  - OK例: node.kind, design_doc.frontmatter["operation_flow"], table_name (generic)
□ 第三者 (osato-lms を知らない開発者) がこの変更で恩恵を受けるか?
```

### 4.2 Default 値チェック (値を設定する変更のみ)

```
□ この default 値で「実使用 100 回中 5 回以上こける」ことはないか?
  → こける見込みがある → default として失格、None (opt-in) にする
□ P99 実使用値 + マージン (1.5〜2倍) で決めているか?
□ ユーザーが env var / codd.yaml で override できるか?
```

CoDD AI timeout の確定値: **3600 秒** (殿確定 2026-05-11)。新規 AI timeout は同値を基準に。

### 4.3 Backward compatibility チェック

```
□ codd.yaml に新フィールドを追加する場合: 未設定時は従来通り動くか (opt-in)?
□ 既存の None / デフォルト挙動を壊していないか?
□ 第三者プロジェクト (codd.yaml を持つ任意プロジェクト) で codd dag verify が壊れないか?
```

---

## Step 5: Implementation

### 5.1 実装ルール

- **osato-lms 固有のノード名・テーブル名を CoDD core にハードコードしない**
- **特定 UI フレームワーク名 (Next.js 等) を CoDD core にハードコードしない**
- `ai_commands.impl_step_derive` 等の設定項目を新設する場合:
  - デフォルト `None` (未設定 = 機能無効) とする
  - 設定なしで codd が silent に失敗しないよう `codd doctor` or 警告ログを出す
- operation_flow を扱う場合: `ai_commands.impl_step_derive` が設定されないと
  `operation_flow_hint()` は呼ばれない。設定欠落を検出する経路を実装すること。

### 5.2 テスト更新

```bash
# 変更に対応するテストを追加・更新
# SKIP=FAIL ルール: 変更後の pytest SKIP=0 が必須
pytest tests/test_<affected_module>.py -v
```

テストのないバグ修正は禁止 (後で同じ bug が再発する)。

---

## Step 6: Dogfood Verification (CoDD 自己適用)

変更後、必ず実 CoDD プロジェクトで動作確認する。任意の実 CoDD プロジェクトを dogfood 環境として使う
(本リポジトリの維持者は osato-lms を標準 dogfood 環境として使用、第三者ユーザは自身のプロジェクトを使用)。

```bash
# 6a. codd-dev pytest (必須)
cd <codd-dev>
pytest --tb=short -q
# → PASS / SKIP=0

# 6b. dogfood プロジェクト DAG verify
cd <dogfood-project>
codd dag verify 2>&1 | tail -10
# → red=0 維持 (変更前と同じか改善)

# 6c. runtime smoke (--runtime 系変更の場合)
codd verify --runtime --runtime-skip verification-test 2>&1 | tail -10
# → EXIT=0

# 6d. 変更が affect した check を dogfood プロジェクトで実際に確認
# 例: ui_coherence 変更なら amber/PASS 状態を確認
# 例: operation_flow_hint 変更なら hint が出力されることを確認
```

**実機確認なしの「完了」報告は禁止**。テスト PASS だけでなく dogfood で動くことを必ず確認する。

---

## Step 7: Versioning

変更後、バージョン番号を決める。

| 種別 | バージョン | 判断基準 |
|---|---|---|
| Bug fix | `PATCH (v2.x.y)` | 既存機能の誤動作を修正のみ |
| Behavior change / Check addition | `MINOR (v2.x.0)` | 機能追加・仕様変更 (BREAKING でも第三者採用ゼロなら MINOR OK) |
| Major architecture / New concept | `MINOR (v2.x.0)` | **v3.0.0 は Shogun が「v3 だ」と言うまで使わない** |

**v3.0.0 への bump は禁止** (Shogun / Lord の明示的決定が必要)。v3 はブランド戦略で決まる
(本リポジトリ維持者の方針 2026-05-10: 第三者採用ゼロのうちは BREAKING でも v2.x MINOR で出す)。

```bash
# pyproject.toml バージョン更新
vi pyproject.toml  # version = "2.x.y"

# CHANGELOG 更新
vi CHANGELOG.md

# commit + tag
git add -A
git commit -m "feat/fix: <summary>"
git tag v2.x.y
git push && git push --tags
```

---

## Step 8: Report

呼び出し元 (orchestrator) に完了報告する。最小限の必須項目:

- 変更種別 (`bug_fix` / `behavior_change` / `check_addition` / `concept_change`)
- pytest 結果 (PASS / SKIP=0 を明示)
- dogfood project の `codd dag verify` 結果 (red=0 維持)
- commit hash (or PR URL)
- (optional) runtime smoke 結果 / 影響範囲メモ

報告経路は環境に依存する:

- **Shogun multi-agent system 経由の場合** (本リポジトリ維持者の運用): 家老/軍師の inbox に
  `inbox_write.sh` で報告
- **直接呼び出し (ユーザ手元)**: terminal 上で結果を要約表示、関連 issue / PR にコメント
- **CI/CD 経由**: PR description / job summary に貼付

経路に関わらず、上記の最小必須項目は必ず含めること。

---

## Forbidden (絶対禁止)

| # | 禁止事項 | 理由 |
|---|---|---|
| F1 | `codd dag verify` red 状態で `codd fix [PHENOMENON]` を実行 | DAG 壊れた状態では修正方針が嘘になる |
| F2 | osato-lms 固有名 (delivery_target / course_master 等) を CoDD core にハードコード | LMS に overfit = 汎用性破壊 |
| F3 | 特定 UI フレームワーク名を CoDD core に入れる | domain agnostic 原則違反 |
| F4 | Shogun 承認なしに新 check / 新 node / 新 edge / 新概念を追加 | CoDD 進化は殿の領域 |
| F5 | default 値を「実使用で頻繁にこける」値に設定 | ユーザーが頻繁にこける default は失格 |
| F6 | 新設定項目を None 以外の非 opt-in デフォルトにする (例: impl_step_derive 等) | silent 有効化で既存プロジェクト破壊 |
| F7 | テストなし / SKIP=1以上 で「完了」報告 | SKIP=FAIL ルール |
| F8 | dogfood 実機確認なしで「完了」報告 | pytest PASS ≠ 実機動作 |
| F9 | CoDD self-hosting の設計 (CoDD 自身を CoDD で開発する) | 新概念連発 + LLM context drift で構造的不可能 |
| F10 | Shogun-tier agent が具体的な CoDD 修正案 (コード差分・関数名・YAML スキーマ案) を提示する | Shogun は戦略レイヤー。具体案を提示すると訓練データの最頻パターン (= 直近 dogfood project) に overfit する。Shogun は phenomenon + constraint のみ宣言し、具体設計は Gunshi (military-strategist) tier で抽象化させる |

---

## Lessons Learned (実証済みの落とし穴)

### L-1: ai_commands.impl_step_derive 欠落による operation_flow_hint 不発 (cmd_344, 2026-05-18)

`codd.yaml` に `ai_commands.impl_step_derive` が未設定の場合、`cli.py` の
`if not explicit and derive_command and nodes:` 条件が偽になり、
`impl_step_deriver.py` 内の `operation_flow_hint()` が呼ばれない。

症状: requirements に `operation_flow` を宣言しても LLM プロンプトに hint が注入されない。
修正: `codd.yaml` に `ai_commands.impl_step_derive: <ai_command>` を追加する。
検出: `codd doctor` での警告 (K-1)、または `impl_step_deriver` での警告 (K-2) で可視化可能。

### L-2: codd verify --runtime の silent hang (cmd_342, 2026-05-17)

`codd verify --runtime` が EXIT=1 + stdout 沈黙でハングする3真因:
1. total timeout 不在 (22 node × 60s = 22 min ハング)
2. `cli.py` の bare `SystemExit` → stdout 沈黙
3. `--runtime-skip verification-test` カテゴリなし

修正: `verify.verification_timeout.total_seconds` + `_emit_verify_summary` + `--runtime-skip` 拡張 (f7727ac)。
osato-lms での推奨コマンド: `codd verify --runtime --runtime-skip verification-test`

### L-3: per-node timeout と total timeout の区別 (cmd_342)

per-node timeout は既に実装済みだった。問題は total timeout がなかったこと。
新規 timeout 実装前に「何が既にあるか」を必ず audit してから設計する。

### L-4: LLM の最小実装収束バイアス (cmd_340/341/343)

LLM は訓練データの最頻パターン (結合 UI) に収束する。`operation_flow` 宣言で
ui_pattern を明示すれば分離 UI が自然に出力される (実証: 実験 B SUCCESS)。
ABD 三位一体: A (operation_flow 宣言) → LLM が分離 UI を出力 → D (ui_coherence) が事後検証。
D だけでは LLM を分離方向に押せない (A なしでも D は suppressed PASS になる)。

### L-5: dogfood での codd dag verify との verify --runtime の使い分け

- `codd dag verify`: 静的整合性チェック。常に実行 OK。
- `codd verify --runtime`: dev server + DB 起動状態が必要。
  `--runtime-skip verification-test` を付けないと verification_test ノードで長時間ハング。
  osato-lms では `codd.yaml` に `verify.verification_timeout` 設定済み。

---

## Configuration Reference (Example Only — Not Required Spec)

以下は **例示** であり、CoDD の必須スキーマではない。第三者ユーザは自身の環境・要件に合わせて
`codd.yaml` を構成する。本例は本リポジトリ維持者の dogfood project (osato-lms) で実際に動作している構成。

```yaml
# codd/codd.yaml
ai_command: codex exec --full-auto --model gpt-5.5 -c 'reasoning_effort="xhigh"' -
ai_commands:
  derive_considerations: claude --print
  impl_step_derive: "codex exec --full-auto --model gpt-5.5 -c 'reasoning_effort=\"xhigh\"' -"
  # impl_step_derive が未設定だと operation_flow_hint() が呼ばれない (L-1)

verify:
  verification_timeout:
    per_node_seconds: 30    # 環境に応じて調整 (WSL2 dev env では 30s 程度で安定)
    total_seconds: 120      # node 数 × per_node_seconds を上回らない範囲で設定
```

ポイント:
- `ai_command` / `ai_commands.*` は任意の AI CLI で代替可 (Claude / Codex / Copilot / Kimi 等)
- timeout 値は dogfood project の実測 P99 + マージン (1.5〜2倍) で決定
- 第三者プロジェクト用 default 値は memory `feedback_codd_default_values_policy` の原則に従う
