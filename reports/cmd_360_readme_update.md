# cmd_360 — CoDD 3-language README v2.20.0 update

**Date**: 2026-05-18
**Scope**: README.md (en) / README_ja.md (ja) / README_zh.md (zh)
**Source of truth**: CHANGELOG.md v2.17.1 → v2.20.0

## Summary

Bring all three READMEs up to date with the OSS-ization wave (v2.19.0 / v2.20.0) and the four cross-cutting features that landed between v2.18.0 and v2.20.0. Drop the (now-shipped) `v2.19.0 (next)` placeholder and refresh the **next** bucket with the publication-stage items.

## Changes per file

### README.md (en)

- **Roadmap**: Added v2.20.0 (Codex App Server JSON-RPC integration, cmd_357) and v2.19.0 (full OSS-ization, cmd_333) with bullet-level coverage of the five cross-cutting features (`--runtime`, `--runtime-skip`, `verification_timeout`, `impl_step_derive` warning, `operation_flow:` frontmatter, `ui_coherence_for_one_to_many`). Added the `codd skills` CLI item and the `codd-evolve` skill bullet ahead of v2.18.0. Replaced the `v2.19.0 (next)` placeholder with a `next` bucket (PHENOMENON auto-propagation, App-Server benchmark publication, lexicon plug-in marketplace).
- **What it does**: Added four new rows under the CI gate row — `codd verify --runtime`, `codd skills {install,list,remove}`, codd-evolve skill, Codex App Server backend (v2.20.0).

### README_ja.md

- Roadmap: 同等の更新 (v2.20.0 / v2.19.0 / codd skills CLI / codd-evolve スキル / v2.18.0)、`v2.19.0 (次期)` → 「次期」(PHENOMENON 自動波及 / App Server ベンチマーク公開 / lexicon マーケットプレイス) に置換。
- できること: en と同じ 4 行を CI ゲート行の下に追加。

### README_zh.md

- Roadmap: 同样的更新 (v2.20.0 / v2.19.0 / codd skills CLI / codd-evolve skill / v2.18.0)，`v2.19.0 (规划中)` → 「规划中」(PHENOMENON 自动波及 / App Server 基准测试公开 / lexicon 市场) に置换。
- 能做什么: en と同じ 4 行を CI 网关行の下に追加。

## Verification

- 3 ファイル文字列 grep: `v2.20.0` / `v2.19.0` / `codd skills` / `codd verify --runtime` / `codd-evolve` / `operation_flow` / `Codex App Server` のいずれも en/ja/zh 各 README に出現
- 固有名 (FLUX / 大里 / osato) は導入していない (Case study の既存「Next.js + Prisma + PostgreSQL multi-tenant LMS」表現は汎用と判断、追加削除なし)
- CHANGELOG.md と矛盾なし (v2.20.0 entry を本文に直接参照)

## Notes

- CoDD CLI は本作業中に実行していない (文書更新のみ、forbidden 遵守)
- pyproject.toml / CHANGELOG.md は既に v2.20.0 (commit `e9ecd53`) で更新済 — 本 cmd は README 同期のみ
- Case study セクションは「real-world LMS」の表現を維持。Zenn 記事 A 流入読者の文脈とも整合
