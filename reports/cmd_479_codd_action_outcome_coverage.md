# cmd_479 CoDD Action Outcome Coverage

## Summary

Implemented generic Action Outcome Coverage in CoDD core. The change keeps the
existing `crud-flow` runtime category intact and adds the broader
`runtime.action_outcome_targets` + `action-outcome` category for any executable
action/command/control that must produce an observable outcome.

The implementation is intentionally framework-agnostic. It does not add
project-specific route, entity, UI label, URL, or dogfood-project logic to core.

## Core Changes

- Added `codd/action_outcome.py` with pure helpers to:
  - extract mutating action requirements from `operation_flow`
  - classify broad verbs such as `manage_collection`
  - compare required actions with `runtime.action_outcome_targets` metadata
- Added runtime config models:
  - `ActionOutcomeTargetConfig`
  - `ActionSpecConfig`
  - `OutcomeExpectationConfig`
- Added `ActionOutcomeChecker`:
  - command target: project-owned command passes on exit code 0
  - HTTP target: declarative `invoke` + `observe` with status/text assertions
- Added runtime report action/outcome matrix rendering.
- Added `action-outcome` to `--runtime-skip`.
- Updated `codd doctor` so:
  - action outcome targets can suppress generic mutating endpoint warnings
  - `operation_flow` action coverage is checked against action outcome metadata
  - legacy `crud-flow` targets alone do not cover update/delete or non-CRUD command actions

## Config Shape

```yaml
runtime:
  action_outcome_targets:
    - name: "record update reflects after reload"
      actions:
        - id: "record.update"
          verb: "update"
          target: "record"
          trigger: "browser submit or API request"
          outcomes:
            - server_acceptance
            - persisted_change
            - visible_reflection
            - reload_persistence
      command: "npx playwright test tests/smoke/record-update.spec.ts"
    - name: "record publish emits event"
      action:
        id: "record.publish"
        verb: "publish"
        target: "record"
        outcomes:
          - server_acceptance
          - emitted_event
      invoke:
        method: POST
        url: "/api/records/publish"
        expected_status: 202
      observe:
        url: "/api/events"
        expected_status: 200
        expect_text: "record.publish"
```

## Tests

Focused:

```text
python3 -m pytest tests/test_action_outcome.py tests/test_runtime_smoke.py tests/test_operation_flow.py -q
38 passed, SKIP=0
```

Full:

```text
python3 -m pytest tests/ -q --tb=no
3041 passed, 30 warnings, SKIP=0
```

CoDD verify:

```text
python3 -m codd.cli verify --path /home/tono/codd-dev
DAG checks: 8 PASS / 0 FAIL (red) / 0 WARN (amber)
Verification tests: 0 PASS / 0 FAIL / 0 SKIP / 0 total
```

Additional checks:

```text
python3 -m codd.cli doctor --path /home/tono/codd-dev
CoDD doctor: PASS

git diff --check
PASS
```

Generality check:

```text
git diff --unified=0 -- codd tests/test_action_outcome.py tests/test_runtime_smoke.py tests/test_operation_flow.py \
  | rg "^\+.*(course|delivery_target|tenant|facility|osato|大里|144\.91|admin/courses)"
0 matches
```

## Files Modified

- `codd/action_outcome.py`
- `codd/runtime_smoke/config.py`
- `codd/runtime_smoke/checks.py`
- `codd/runtime_smoke/runner.py`
- `codd/runtime_smoke/report.py`
- `codd/cli.py`
- `codd/defaults.yaml`
- `codd/templates/codd.yaml.tmpl`
- `tests/test_action_outcome.py`
- `tests/test_runtime_smoke.py`
- `tests/test_operation_flow.py`
- `docs/requirements/extract-verify-requirements.md`
- `docs/design/verify-architecture.md`
- `skills/codd-evolve/SKILL.md`
- `README.md`
- `README_ja.md`
- `README_zh.md`
- `CHANGELOG.md`
- `reports/cmd_479_codd_action_outcome_coverage.md`

## Residual Risks

- Project-owned command targets still rely on the project test command to prove
  the actual behavior. CoDD now records the intended action/outcome matrix, but it
  cannot inspect arbitrary test internals yet.
- `manage_collection` remains an explicit-warning case when the project does not
  declare create/update/delete metadata. This is intentional because inferring the
  exact action set from a broad verb would be unsafe.
- Static inert-control detection is not included in this implementation. The core
  guarantee is operation_flow-to-runtime evidence mapping, not framework-specific
  DOM handler analysis.
