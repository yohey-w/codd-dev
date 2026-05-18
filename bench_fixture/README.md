# bench_fixture (cmd_364)

cmd_364 で行った "direct AiCommand benchmark" の作業ディレクトリプレースホルダ。

`scripts/bench_app_server_direct.py` は `--project-root` を `AiCommand`
インスタンスに渡すが、本 fixture は **コード生成を伴わない** ため
ファイル内容は不要。ディレクトリの存在だけが重要 (`SubprocessAiCommand`
が `cwd` に使う)。

再現:

```bash
python3 scripts/bench_app_server_direct.py \
  --backend both \
  --concurrency 1,10,30 \
  --invocations 30 \
  --turns-per-invocation 3 \
  --output scripts/cmd_364_results.jsonl
```

詳細結果: `/path/to/orchestrator/reports/cmd_364_codex_app_server_benchmark_v2.md`
