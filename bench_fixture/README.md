# bench_fixture

A working-directory placeholder for the `direct AiCommand` benchmark.

`scripts/bench_app_server_direct.py` passes `--project-root` to the `AiCommand`
instance, but this benchmark does **not** generate code, so the directory's
contents are irrelevant — only its existence matters (`SubprocessAiCommand`
uses it as `cwd`).

Reproduce:

```bash
python3 scripts/bench_app_server_direct.py \
  --backend both \
  --concurrency 1,10,30 \
  --invocations 30 \
  --turns-per-invocation 3 \
  --output scripts/bench_results.jsonl
```

Detailed results are written to the `--output` path you choose.
