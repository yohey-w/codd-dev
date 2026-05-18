# cmd_358 - Codex App Server Benchmark Runbook

This runbook explains how to prepare and run `scripts/bench_app_server.py` without changing project state during dry runs.

## Preconditions

- Use a CoDD-initialized target project such as `osato-lms`.
- Confirm `codex --version`, `codd --version`, and ChatGPT authentication before real measurements.
- Do not run real measurements until Lord approval. Use `--dry-run` for script validation.

## Codex App Server Configuration

For stdio transport, configure the target project's `codd/codd.yaml` or `.codd/codd.yaml`:

```yaml
codex_app_server:
  enabled: true
  transport: stdio
  thread_strategy: per_session
  effort: xhigh
  model: gpt-5.5
  timeout_seconds: 300
  fallback: subprocess
```

For unix socket transport, start a daemon in another terminal:

```bash
codex app-server --listen unix:///tmp/codex-app-server.sock
```

Then configure:

```yaml
codex_app_server:
  enabled: true
  transport: unix
  url: unix:///tmp/codex-app-server.sock
  thread_strategy: per_cmd
  fallback: subprocess
```

## Script Validation

```bash
python3 scripts/bench_app_server.py \
  --dry-run \
  --target implement \
  --backend subprocess \
  --concurrency 1 \
  --rounds 1 \
  --warmup 0 \
  --project-root /home/tono/osato-lms
```

## Baseline Subprocess Run

After approval:

```bash
python3 scripts/bench_app_server.py \
  --target all \
  --backend subprocess \
  --concurrency 1,10,50 \
  --rounds 5 \
  --project-root /home/tono/osato-lms
```

## Subprocess vs App Server

After approval and app-server configuration:

```bash
python3 scripts/bench_app_server.py \
  --target all \
  --backend both \
  --concurrency 1,10,50 \
  --rounds 5 \
  --transport auto \
  --project-root /home/tono/osato-lms
```

Concurrency 100 requires an explicit guardrail:

```bash
python3 scripts/bench_app_server.py \
  --target implement \
  --backend both \
  --concurrency 100 \
  --rounds 1 \
  --allow-high-concurrency \
  --project-root /home/tono/osato-lms
```

## Outputs

- Raw JSONL: `.codd/bench/raw_YYYYMMDD_HHMMSS.jsonl`
- Markdown summary: `.codd/bench/summary_YYYYMMDD_HHMMSS.md`

Read the summary table first. A fallback rate above 10% means the app-server environment is unhealthy. Rate-limited cells are marked as skipped and should not be treated as completed measurements.
