# cmd_368 — Runtime CRUD Flow Verification

## Summary

CoDD now supports an opt-in runtime CRUD flow category for `codd verify --runtime`.
The category is configured with `runtime.crud_flow_targets` and verifies project-owned
create/update/delete reflection flows without hardcoding any project endpoint.

This closes the cmd_366/cmd_367 blind spot where ordinary runtime smoke could pass
without proving `POST -> persistence -> re-fetch -> visible list/detail reflection`.

## Changes

- Added `runtime.crud_flow_targets` config parsing.
- Added `CrudFlowChecker` with two target modes:
  - project-owned `command`
  - declarative HTTP `create` + `reflect` polling with optional `expect_text`
- Added the `crud-flow` runtime smoke category and `--runtime-skip crud-flow`.
- Added `codd doctor --path` warning for POST-like source handlers without CRUD reflection config or reflection-oriented E2E tests.
- Tightened `codd doctor` so POST status-only tests do not suppress the CRUD reflection warning.
- Adjusted the `codd.yaml` template so the CRUD flow example can be uncommented as valid YAML.
- Updated `codd-evolve` Step 8 to require mutate -> persistence/server acceptance -> re-fetch -> visible reflection evidence for POST/PUT/PATCH/DELETE changes.
- Updated `/home/tono/.claude/skills/codd-improve/SKILL.md` with Lesson L-6.
- Updated README en/ja/zh, CHANGELOG, requirements, architecture, defaults, and codd.yaml template.

## Files Changed

- `codd/runtime_smoke/config.py`
- `codd/runtime_smoke/checks.py`
- `codd/runtime_smoke/runner.py`
- `codd/cli.py`
- `codd/defaults.yaml`
- `codd/templates/codd.yaml.tmpl`
- `tests/test_runtime_smoke.py`
- `docs/requirements/extract-verify-requirements.md`
- `docs/design/verify-architecture.md`
- `README.md`
- `README_ja.md`
- `README_zh.md`
- `CHANGELOG.md`
- `skills/codd-evolve/SKILL.md`
- `/home/tono/.claude/skills/codd-improve/SKILL.md`
- `/home/tono/osato-lms/codd/codd.yaml` (dogfood target)

## Verification

- `python3 -m pytest tests/test_runtime_smoke.py -q`
  - PASS: 23 passed
- `python3 -m pytest tests/ -q --tb=no`
  - PASS: 3030 passed, 30 warnings, SKIP 0
- `python3 -m codd.cli verify --path /home/tono/codd-dev`
  - PASS: DAG 8 PASS / 0 FAIL (red) / 0 WARN (amber); verification tests 0 total
- `python3 -m codd.cli dag verify --path /home/tono/codd-dev`
  - PASS: red 0; existing amber warning remains for transitive/unreachable and propagation-output context
- `python3 -m codd.cli doctor --path /home/tono/osato-lms`
  - PASS: `CoDD doctor: PASS`
- Template YAML smoke:
  - PASS: uncommented `runtime.crud_flow_targets` example parses as a list of two targets

## QC Redo Fixes

Gunshi QC returned `CHANGES_REQUESTED` for two issues. Both are fixed:

- `doctor_false_negative_post_status_only`
  - Before: a POST test with only `expect(response.status()).toBe(201)` could suppress the warning.
  - After: bare `expect(` is no longer a reflection marker, and `request.post(...)` is recognized as a POST-like test action.
  - Tests added:
    - status-only POST test => `CoDD doctor: WARN`
    - POST + `getByText(...).toBeVisible()` reflection test => `CoDD doctor: PASS`
- `template_example_yaml_shape`
  - Before: `crud_flow_targets: []` sat next to commented list-item examples.
  - After: the example uses `crud_flow_targets:` with list items under it, so uncommenting the block yields valid YAML.

## Dogfood

Configured `/home/tono/osato-lms/codd/codd.yaml`:

```yaml
runtime:
  crud_flow_targets:
    - name: delivery target add form reflects in registered list
      command: 'npx playwright test tests/smoke/course_delivery_separation_smoke.spec.ts --project=desktop-1920 --reporter=line -g "admin delivery target add form"'
      env:
        SMOKE_BASE_URL: "{{dev_server_url}}"
      timeout: 120
```

Target command:

- `SMOKE_BASE_URL=http://localhost:3000 npx playwright test tests/smoke/course_delivery_separation_smoke.spec.ts --project=desktop-1920 --reporter=line -g "admin delivery target add form"`
- PASS: 1 passed

Runtime smoke runner with the osato-lms config:

- Report: `/home/tono/osato-lms/reports/runtime_smoke_20260519_110733.md`
  - DB: PASS
  - dev-server: PASS
  - connectivity: PASS
  - CRUD flow: PASS
  - overall: FAIL because existing broad `runtime_smoke.e2e.command: npx playwright test tests/smoke/` failed in login/navigation tests
- Report: `/home/tono/osato-lms/reports/runtime_smoke_20260519_111136.md`
  - DB: PASS
  - dev-server: PASS
  - connectivity: PASS
  - CRUD flow: PASS
  - overall: FAIL for the same existing broad E2E login/navigation failures

Conclusion: the new `crud-flow` category detects and verifies the cmd_367 POST reflection path and is green. The full osato-lms runtime smoke overall result is currently blocked by separate existing login/navigation smoke failures, not by the CRUD category.

## Compatibility

Existing projects without `runtime.crud_flow_targets` keep the previous runtime behavior. The new category is opt-in and only runs when targets are configured, or records an explicit skipped category if `--runtime-skip crud-flow` is supplied.

## Residual Risk

The osato-lms full `runtime_smoke.e2e.command` is unstable/failing in login/navigation tests in the current local environment. This is outside the cmd_368 CRUD flow implementation but prevents claiming an overall osato-lms runtime smoke green without qualification.
