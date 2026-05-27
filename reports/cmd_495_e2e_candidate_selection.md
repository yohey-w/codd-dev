# cmd_495 E2E Candidate Selection CLI Integration

Task: subtask_495_a2
Worker: ashigaru4
Timestamp: 2026-05-27T22:24:01+09:00

## Summary

`codd coverage-obligations` now wires the core
`candidate_selection_payload(...)` result into JSON and Markdown output.

The stale `not_implemented_in_cmd_494` future TODO payloads were removed from
CLI output and replaced with:

- `generated_e2e_candidates`
- `selected_e2e_suite`
- `unselected_e2e_candidates`
- `excluded_obligations`
- `required_obligation_ids`
- `required_without_selected_candidate_ids`

The CLI trace matrix keeps the original extracted `coverage_status` and
`covered_by` values, and only adds planning IDs:

- `generated_candidate_ids`
- `selected_candidate_ids`
- `excluded_reason`
- `exclusion_reason`

Selected suite entries are planning artifacts only. They do not change
`coverage_status`, populate `covered_by`, or count as green coverage.

`codd doctor` and `codd verify` also print a concise coverage-obligation planning
summary, including generated candidate count, selected planning entry count,
exclusion count, and the planning-only warning.

## Tests

- `uv run pytest -q tests/test_coverage_obligation_cli.py`: PASS, 4 passed / SKIP 0
- `uv run pytest -q tests/test_coverage_obligation_cli.py tests/test_coverage_e2e_selection.py tests/test_coverage_obligations.py`: PASS, 19 passed / SKIP 0
- `uv run codd doctor --path .`: PASS, planning summary emitted
- `uv run codd verify`: PASS, red 0 / SKIP 0, planning summary emitted
- `uv run python -m py_compile codd/cli.py tests/test_coverage_obligation_cli.py`: PASS
- `git diff --check -- codd/cli.py tests/test_coverage_obligation_cli.py docs/design/coverage-obligation-driven-e2e-v0.md reports/cmd_495_e2e_candidate_selection.md`: PASS
- `grep -E -n 'osato|大里|tenant_admin|learner|central_admin|delivery-target' codd/cli.py codd/coverage_e2e_selection.py || true`: PASS, no matches

## Fixture Coverage

CLI tests now assert:

- JSON returns real candidate and selected suite lists.
- CLI output no longer contains `not_implemented_in_cmd_494`.
- Trace rows remain `uncovered` even when generated and selected candidate IDs exist.
- Markdown renders generated candidate and selected suite tables.
- Valid lower-level delegation is excluded from candidate generation and appears in
  `excluded_obligations`.

Active waiver exclusion is covered by the core API test
`test_active_waiver_is_excluded_but_expired_waiver_is_candidate`. The current
extractor does not preserve waiver fields from CLI fixtures for runtime targets,
so duplicating that case at the CLI layer would require extractor changes outside
this slice.

## Runtime Smoke

Runtime smoke is N/A for this CLI integration slice. The work only connects
planning payloads into `coverage-obligations` output and does not execute a
selected E2E suite against a running app. This is intentionally not reported as
PASS.
