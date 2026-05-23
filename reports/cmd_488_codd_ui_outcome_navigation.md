# cmd_488 CoDD Generic UI Outcome / Navigation Report

## Summary

cmd_486/cmd_487 were treated as generic CoDD defects, not LMS-specific defects.
The CoDD core now warns when terminal/non-repeatable actions lack post-action
control-state evidence, and when visible business screens lack an escape route
or inherited navigation evidence.

## Root Cause

- Visible command controls could be covered by weak outcomes such as visible text
  or reload persistence without proving that the control is absent, disabled, or
  otherwise safe after a terminal action.
- Authenticated business screens could exist outside persistent navigation
  shells without CoDD warning that the user had no home/back/dashboard route.

## Core Changes

- `codd/action_outcome.py`
  - Added generic `complete` verb aliases.
- `codd/cli.py`
  - Added terminal action outcome warnings for `complete`, `delete`, `disable`,
    `archive`, and `revoke`.
  - Added screen escape-route warnings for page/screen/view/route files with
    visible content but no local or ancestor navigation evidence.
  - Added terminal outcome names such as `disabled_state`, `control_absence`,
    `expected_absence`, and `terminal_state_guard`.
- `tests/test_action_outcome.py`
  - Added coverage proving `complete` is recognized as a mutating terminal verb.
- `tests/test_runtime_smoke.py`
  - Added doctor tests for terminal control-state warnings.
  - Added doctor tests for business screen escape-route warnings.
- `docs/requirements/extract-verify-requirements.md`,
  `docs/design/verify-architecture.md`, and `codd/templates/codd.yaml.tmpl`
  document the generic contracts.

## Dogfood Evidence

Before the LMS fix, fixed CoDD reported both target classes generically:

- `/tenant/progress`: `tenant_progress_complete` declared terminal/non-repeatable
  `complete` but did not assert post-action control state.
- `/notifications`: business screen had visible page content but no static
  escape route/navigation evidence.

After the LMS fix, fixed CoDD no longer reports those two target violations.
Remaining warnings are unrelated pre-existing findings:

- synthetic mutation warnings in admin workbench components
- standalone escape-route warnings for login/forbidden screens

## Verification

- `python3 -m pytest tests/test_action_outcome.py tests/test_runtime_smoke.py -q`
  - Result: `49 passed`; SKIP=0.
- `python3 -m pytest tests/llm/test_generality_gate_two_layer.py -q`
  - Result: `6 passed`; SKIP=0.
- Diff-only overfit grep:
  - Pattern: `osato|大里|tenant|learner|course|delivery-target|delivery_target|144\.91|LMS|お知らせ|進捗|修了`
  - Result: no matches in the CoDD core/docs/test diff.
