# Claude Code x CoDD セットアップガイド

CoDDをClaude Codeに組み込み、依存スキャンとバリデーションを通常の編集ループに統合する。一度設定すれば、**グラフのメンテナンスを意識する必要は二度とない** — フックが自動で処理する。

## クイックスタート（5分）

1. CoDDをインストール
2. CoDD SkillをClaude Codeに登録
3. プロジェクトレベルのフックを `.claude/settings.json` に追加
4. git `pre-commit` フックをインストール
5. CoDDの標準ループで開発開始: init → generate → scan → impact

## 1. CoDD��インストール

```bash
pip install codd-dev
```

CLIが利用可能か確認:

```bash
codd --help
```

## 2. CoDD SkillをClaude Codeに登録

Claude Codeの設定（`~/.claude/settings.json` または `.claude/settings.json`）にCoDDのSkillディレクトリを追加:

```json
{
  "skillsPath": [
    "<codd-devのパス>/skills"
  ]
}
```

これで全CoDD Skillがスラッシュコマンドとして登録される:

| Skill | 機能 |
|-------|------|
| `/codd-init` | プロジェクト初期化 + 要件インポート |
| `/codd-generate` | HITLゲート付きWave順設計書生成（グリーンフィールド） |
| `/codd-restore` | 抽出事実から設計書復元（ブラウンフィールド） |
| `/codd-scan` | フロントマターから依存グラフ再構築 |
| `/codd-impact` | Green/Amber/Grayプロトコルで変更影響分析 |
| `/codd-validate` | フロントマター & 依存関係の整合性チェック |

## 3. プロジェクトフックを `.claude/settings.json` に追加

プロジェクトルートに `.claude/settings.json` を作成。チーム全員が同じCoDD自動化を共有できる:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/install-codd-pre-commit.sh"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "async": true,
            "command": "cd \"$CLAUDE_PROJECT_DIR\" && codd scan --path ."
          }
        ]
      }
    ]
  }
}
```

各フックの役割:

- **SessionStart**: Claude Codeがプロジェクトを開く/再開するたびに、gitの `pre-commit` フックをインストール済みに保つ。
- **PostToolUse**: ファイル編集のたびに `codd scan` を再実行。**依存グラフは常に最新 — `codd scan` を手動で叩く必要は二度とない。**

## 4. Git `pre-commit` フックをインストール

`.claude/hooks/install-codd-pre-commit.sh` を作成:

```bash
#!/usr/bin/env bash
set -euo pipefail

HOOK_PATH="${CLAUDE_PROJECT_DIR}/.git/hooks/pre-commit"

cat > "$HOOK_PATH" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

echo "[CoDD] validating dependency graph before commit..."
codd validate --path .
EOF

chmod +x "$HOOK_PATH"
```

インストーラを実行可能にする:

```bash
chmod +x .claude/hooks/install-codd-pre-commit.sh
```

`settings.json` 経由で登録する理由:

- 新しいメンバーが手動セットアップを覚えなくても同じバリデーションフックが適用される。
- インストーラスクリプトを更新すれば、次のClaude Codeセッション開始時にリポジトリ全体の `pre-commit` 動作が更新される。

## 5. エンドツーエンド ワークフロー

### CLIワークフロー

```bash
# 1. 要件定義ファイルを渡して初期化（形式自由: txt, md, doc）
codd init --project-name "my-project" --language "typescript" \
  --requirements spec.txt

# 2. AIが設計書を生成（wave_configも自動生成）
codd generate --wave 2

# 3. 依存グラフを構築
codd scan --path .

# 4. 要件や設計書を編集...

# 5. 影響範囲を確認（未コミットの変更を自動検知）
codd impact --path .

# 6. コミット前にバリデーション（pre-commitフックでも自動実行）
codd validate --path .
```

### Skillワークフロー（推奨）

Skillを使えば、Claudeがフラグを処理しHITLゲートを自動で追加する:

```
殿:  /codd-init
     → Claudeが--requirementsで初期化、フロントマターを自動付与

殿:  /codd-generate
     → ClaudeがWave 2を生成、出力をレビュー、承認を確認
     → 「Wave 2の設計書を確認しました。Wave 3に進みますか？」

殿:  はい

殿:  （要件を編集 — 新機能を追加）

殿:  /codd-impact
     → Claudeが変更を検知、Green/Amber/Grayプロトコルに従う
     → Green帯域: 安全な設計書を自動更新
     → Amber帯域: 「test-strategyが影響を受けています。更新しま���か？」
```

### 日々の開発で実際にやること

フックが有効なら、毎日のワークフローはこれだけ:

1. **普通にファイルを編集する。** PostToolUseフックが編集のたびに `codd scan` を実行 — 完全に透明。
2. **影響を知りたくなったら `/codd-impact` を叩く。** グラフは常に最新。
3. **コミットする。** pre-commitフックが `codd validate` を実行 — 整合性が壊れた状態ではコミットできない。

それだけ。グラフのメンテナンスは完全に透明。あなたは開発に集中し、CoDDが一貫性を守る。
