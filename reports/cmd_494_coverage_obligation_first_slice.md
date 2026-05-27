# cmd_494 Coverage Obligation First Slice

## Scope

This first slice adds the `codd coverage-obligations` CLI entrypoint for
obligation trace reporting. It uses the existing schema/model and extractor
slice, then emits:

- `summary`
- `trace_matrix`
- `obligations`
- `evidence_candidates`
- `unsupported_items`
- `generated_e2e_candidates`
- `selected_e2e_suite`

`generated_e2e_candidates` and `selected_e2e_suite` are explicit
`future_todo` / `not_implemented_in_cmd_494` records in this slice. They are
not treated as green coverage.

## CLI Usage

JSON:

```bash
codd coverage-obligations --path <project> --format json
```

Markdown:

```bash
codd coverage-obligations --path <project> --format markdown
```

The JSON output is intended for automation. The Markdown output is intended for
review and includes an obligation matrix table with `obligation_id`, `kind`,
`actor`, `coverage_status`, and `source`.

## Doctor Entrypoint Usage

`codd doctor --path <project>` remains the configuration diagnostic entrypoint.
It does not replace the obligation trace report. A practical first pass is:

```bash
codd doctor --path <project>
codd coverage-obligations --path <project> --format markdown
```

## Baseline Caveat

This slice reports obligations extracted from existing declarations and
conservative inferred gaps. It does not generate E2E tests, select a minimal
suite, run browser smoke tests, or prove runtime behavior. Uncovered rows,
unsupported items, and skipped/future TODO concepts must remain visible in the
report.

## Verification Results

Focused tests:

```bash
uv run pytest -q tests/test_coverage_obligation_cli.py tests/test_coverage_obligations.py
```

Result: PASS, 10 passed, SKIP 0.

Runtime smoke:

Result: N/A. This slice adds a reporting CLI over static/project declarations.
It does not start an application server or execute generated browser tests, and
no selected E2E suite exists yet. This is not recorded as PASS.

Whitespace check:

```bash
git diff --check -- codd/cli.py tests/test_coverage_obligation_cli.py reports/cmd_494_coverage_obligation_first_slice.md docs/design/coverage-obligation-driven-e2e-v0.md
```

Result: PASS.

Compile check:

```bash
uv run python -m py_compile codd/cli.py tests/test_coverage_obligation_cli.py
```

Result: PASS.

Core project-specific vocabulary check:

```bash
grep -R -n "osato\|osato-lms" codd/cli.py codd/coverage_obligations.py codd/coverage_obligation_extractor.py
```

Result: PASS, no hits.

Final integration gate:

```bash
uv run pytest -q tests/test_coverage_obligations.py tests/test_coverage_obligation_extractor.py tests/test_coverage_obligation_cli.py tests/test_user_journey_c7_check.py tests/test_runtime_smoke.py tests/test_coverage_metrics.py tests/test_coverage_auditor.py
uv run pytest -q
uv run codd verify
uv run codd dag verify --format json
uv run codd coverage-obligations --path . --format json
uv run codd coverage-obligations --path . --format markdown
git diff --check
```

Result: PASS. Focused gate 117 passed / SKIP 0. Full pytest 3078
passed / SKIP 0 / 30 warnings. `codd verify` reported DAG 8 PASS /
0 red FAIL / 0 amber WARN and verification tests 0 SKIP. `codd dag verify`
exited 0 with existing caveats: depends_on_consistency has a passed+skipped
message when propagation output is absent, and user_journey_coherence has an
info/pass "C7 SKIP" message when no actors/journeys exist. These are not
reported as green coverage and remain next-slice gate work.

Final QC: Gunshi `subtask_494_g2` verdict `adopt_with_caveats`.

## Next Slice

- Generate `generated_e2e_candidates` from obligations.
- Select `selected_e2e_suite` with risk-aware set-cover rules.
- Connect selected suites to runtime smoke execution and trace evidence.
