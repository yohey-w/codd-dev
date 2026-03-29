# CoDD Init

Initialize CoDD in the current project, then move immediately into the first scan and validation cycle so the dependency graph starts from a clean baseline.

## Usage

Run this skill when a repository does not have a `codd/` directory yet and you need to bootstrap CoDD with the official `codd` CLI entry point.

## Instructions

1. Preflight before running `codd init`:
   - Work from the project root where `codd/` should be created.
   - If `codd/` already exists, do not re-run init. Inspect the existing CoDD setup instead.
   - If the `codd` command is not available, install the package so the console script is on `PATH` before continuing.

2. Collect the two required inputs:
   - Project name: default to the current directory name unless the user provides a better product name.
   - Primary language: choose one of `python`, `typescript`, `java`, or `go`.

3. Choose the command based on project type:
   - Python project:
```bash
codd init --project-name "<project-name>" --language python --dest .
```
   - TypeScript project:
```bash
codd init --project-name "<project-name>" --language typescript --dest .
```
   - Java project:
```bash
codd init --project-name "<project-name>" --language java --dest .
```
   - Go project:
```bash
codd init --project-name "<project-name>" --language go --dest .
```

4. Run the selected `codd init` command.

5. Confirm the generated skeleton:
   - `codd/codd.yaml`
   - `codd/scan/`
   - `codd/reports/`
   - `codd/.gitignore`
   - `.codd_version`

6. For an existing project, handle the bootstrap carefully:
   - Do not assume old design docs already have CoDD frontmatter. Inspect them before the first scan.
   - Prefer adding frontmatter to the documents you want CoDD to track before treating scan results as authoritative.
   - Legacy annotation files under `codd/annotations/` are optional and backward-compatible. Create them only if the project still uses that workflow.

7. Next steps immediately after init:
   1. Register the initial design documents in `codd/annotations/doc_links.yaml` if the project uses legacy annotations. Create `codd/annotations/` first if it does not exist yet, or rely on Markdown frontmatter when the project is frontmatter-first.
   2. Add or review CoDD frontmatter in the requirement and design documents you want included in the graph.
   3. Run the first scan:
```bash
codd scan --path .
```
   4. Validate the graph inputs before moving on:
```bash
codd validate --path .
```
   5. If validation passes, continue the normal operating cycle:
```bash
codd scan --path .
codd impact --diff HEAD~1 --path .
```

8. Report back with:
   - Which language option was used
   - Whether init created the skeleton successfully
   - Which documents were prepared for the first scan
   - Whether `codd validate --path .` passed or which issues remain
