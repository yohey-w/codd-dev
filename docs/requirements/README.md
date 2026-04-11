# CoDD Requirements — 整備中

これらの要件定義書は `codd extract` による自動生成出力です。
人間によるレビューは未実施のため、内容の正確性は保証されません。

- **confidence: 0.65** = 機械生成デフォルト（未レビュー）
- **confidence: 1.0** にするには全 [speculative] 項目の確認が必要

## 未解決の確認事項

- [ ] バージョン不整合（`.codd_version: 0.2.0` vs `pyproject.toml: 1.7.0`）
- [ ] テストカバレッジ0%モジュール（generator, validator, planner, FeatureCluster）
- [ ] clustering confidence定数の妥当性（0.3, 0.4, 0.1, 0.2）
- [ ] Java/SQL tree-sitter対応状況
- [ ] `codd fix` システムプロンプト問題
- [ ] contracts substring false positive
- [ ] [speculative] 12件、矛盾6件、カバレッジ欠落8件の精査
