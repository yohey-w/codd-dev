# cmd_489 Global Action Responsive Coverage

## Summary

Implemented a generic CoDD guard for authenticated responsive UIs where required global actions can disappear on compact surface variants. The new runtime category is `global-action`, backed by `runtime.global_action_targets`.

## Root Cause

CoDD had journey and action outcome checks, but no first-class contract saying that authenticated global actions must remain available across responsive layout substitutions. A desktop sidebar action could therefore satisfy app behavior informally while the compact layout lost the same session action.

## CoDD Changes

- Added `runtime.global_action_targets` config loading.
- Reused the action outcome runner for a new `global-action` runtime category.
- Added CLI support for `--runtime-skip global-action`.
- Added doctor warning when authenticated responsive UI exists without a global action target.
- Updated docs and template with neutral session/action examples.

## Verification

- `python3 -m pytest tests/test_runtime_smoke.py -q` -> 47 passed, SKIP=0.
- `python3 -m pytest tests/test_runtime_smoke.py tests/test_action_outcome.py tests/llm/test_generality_gate_two_layer.py -q` -> 60 passed, SKIP=0.
- `git diff --check` -> PASS.
- Diff-only overfit grep for LMS/project terms -> no added hits.
- Dogfood doctor on `/tmp/osato-lms-cmd485` after the app target was declared no longer reports the new `runtime.global_action_targets` warning; only pre-existing unrelated warnings remain.

## Dogfood Result

`run_runtime_smoke(..., base_url_override="http://localhost:3106")` was executed against `/tmp/osato-lms-cmd485` with the local dev server database URL injected from the running dev-server environment. Final result:

- Overall: PASS
- Real-browser E2E: 39 passed
- `global-action`: PASS
- Global action command: 3 passed on `smartphone-se`
- Runtime report: `/tmp/osato-lms-cmd485/reports/runtime_smoke_20260524_005930.md`

An earlier run without `DATABASE_URL` failed only in pre-existing DB-backed smoke targets. It was not accepted as evidence.
