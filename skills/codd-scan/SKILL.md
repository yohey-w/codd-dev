# CoDD Scan

Refresh the CoDD dependency graph from document frontmatter and source code so later commands (`codd validate`, `codd impact`, `codd generate`, `codd verify`) operate on current project state. Use this after requirements, design docs, implementation, or tests change.

## When to Use

- After editing Markdown files with CoDD frontmatter
- After adding or changing source files that affect dependencies
- Before `codd impact` when you need a fresh graph
- When scan counts look stale or unexpectedly low

## Preconditions

1. Resolve the target project root first. Prefer an absolute path when the working directory is ambiguous.
2. Check whether `codd/codd.yaml` exists under that path.
3. If `codd/codd.yaml` is missing, initialize CoDD before scanning:

```bash
codd init --project-name "<project-name>" --language "<language>" --dest <path>
```

4. If `codd` is not on `PATH`, use one of these:
   - `pip install codd-dev`
   - `.venv/bin/codd scan --path <path>`

## Command

Run the scan from anywhere by passing the project root explicitly:

```bash
codd scan --path <path>
```

If you are already at the project root, `codd scan --path .` is sufficient.

## What the Scan Does

1. Reads `codd/codd.yaml`
2. Rebuilds auto-generated graph data from project documents and source code
3. Preserves human-authored evidence
4. Prints a short summary of frontmatter coverage and graph size

## How to Interpret the Result

Typical output includes:

```text
Scan complete:
  Documents with frontmatter: <count>
  Graph: <nodes> nodes, <edges> edges
  Evidence: <total> total (...)
```

- `Documents with frontmatter`: how many Markdown documents in configured `doc_dirs` were successfully read as CoDD documents. If this is lower than expected, inspect missing frontmatter or wrong `doc_dirs`.
- `nodes`: the number of tracked artifacts in the graph. In document-centric projects this is usually close to the total number of requirements/design/test artifacts being tracked.
- `edges`: the total number of declared or inferred dependency relationships between those artifacts.
- `orphan nodes`: artifacts that have no surviving dependency relationship. They are often a sign of missing `depends_on`, stale docs, or a document that should be deleted. Some top-level requirements may be intentional exceptions, but unexplained orphans should always be reviewed.
- `WARNING:` lines: scan found suspicious input such as missing frontmatter or design docs with empty dependencies. Treat warnings as a prompt to run `codd validate`.
- `SCAN_ERROR` (if surfaced by your wrapper, report script, or higher-level UI): frontmatter could not be parsed or read cleanly. Treat it as a validation problem and inspect the document with `codd validate --path <path>`.

## Recommended Next Actions

Use this decision guide after every scan:

| Condition | Next action |
|-----------|-------------|
| Scan completed and counts look plausible | Run `codd validate --path <path>` to confirm references and frontmatter integrity |
| Scan printed warnings or your tooling reports `SCAN_ERROR` | Run `codd validate --path <path>` immediately and fix the reported documents before moving on |
| Orphan nodes are present | Check whether they are intentional. If not, add missing dependency metadata or remove obsolete docs, then rerun `codd scan` and `codd validate` |
| You already trust the graph and now need blast-radius analysis for recent changes | Run `codd impact --path <path>` |
| You need impact for a specific commit or branch delta | Run `codd impact --diff <git-ref> --path <path>` |

## Autonomous Workflow

When using this skill without further user guidance:

1. Confirm the project root and `codd/codd.yaml`
2. If CoDD is not initialized, run `codd init ... --dest <path>` first
3. Run `codd scan --path <path>`
4. Read the summary and warnings
5. Run `codd validate --path <path>` if anything looks wrong, incomplete, or newly changed
6. Run `codd impact --path <path>` only after the graph is fresh and you need downstream impact analysis
7. Report the scan summary in plain language: frontmatter coverage, node count, edge count, warnings, and whether follow-up validation or impact analysis is needed

## Troubleshooting

### `No such file` or path-related failures

- Verify `<path>` points at the project root, not a subdirectory
- Check that `codd/` exists under the target path
- Retry with an absolute path

### `codd: command not found`

- Install the CLI: `pip install codd-dev`
- Or run the virtualenv binary directly: `.venv/bin/codd scan --path <path>`

### `Error: codd/ not found` or `codd/codd.yaml not found`

- Initialize CoDD first:

```bash
codd init --project-name "<project-name>" --language "<language>" --dest <path>
```

- Then confirm `codd/codd.yaml` and configured `doc_dirs`/`source_dirs` before scanning again

### Frontmatter parse errors or `SCAN_ERROR`

- Run:

```bash
codd validate --path <path>
```

- Fix malformed YAML frontmatter, undefined references, or circular dependencies
- Rerun `codd scan --path <path>` after the validation issues are resolved
