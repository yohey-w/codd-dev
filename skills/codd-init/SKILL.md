# CoDD Init

Initialize CoDD in the current project, then move immediately into the first scan and validation cycle so the dependency graph starts from a clean baseline.

## Usage

Run this skill when a repository does not have a `codd/` directory yet and you need to bootstrap CoDD with the official `codd` CLI entry point.

## Instructions

1. Preflight before running `codd init`:
   - Work from the project root where `codd/` should be created.
   - If `codd/` already exists, do not re-run init. Inspect the existing CoDD setup instead.
   - If the `codd` command is not available, install the package so the console script is on `PATH` before continuing.

2. Collect the required inputs:
   - Project name: default to the current directory name unless the user provides a better product name.
   - Primary language: choose one of `python`, `typescript`, `java`, or `go`.
   - Requirements file (optional): if the user already has a requirements document (any format — `.txt`, `.md`, `.doc`), use `--requirements` to import it. CoDD adds frontmatter automatically.

3. Choose the command:
   - With existing requirements file (recommended):
```bash
codd init --project-name "<project-name>" --language <language> --requirements <path-to-requirements> --dest .
```
   - Without requirements (user will add them later):
```bash
codd init --project-name "<project-name>" --language <language> --dest .
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

7. Next steps immediately after init — choose greenfield or brownfield:

   ### Greenfield (have requirements, no code yet)

   1. If you used `--requirements`, the requirements document is already in `docs/requirements/requirements.md` with CoDD frontmatter. Skip to step 2.
      If you did NOT use `--requirements`, write a requirements document (plain text is fine) and import it into the existing project:
```bash
codd init --project-name "<project-name>" --language "<language>" \
  --requirements <path-to-requirements> --dest .
```
      This works even when `codd/` already exists — it only imports the file and adds frontmatter.
      Or manually create `docs/requirements/requirements.md` with frontmatter (`node_id` and `type: requirement`).
   2. Run `codd generate` to auto-generate wave_config and design docs:
```bash
codd generate --wave 2 --path .
```
   3. Scan to build the dependency graph:
```bash
codd scan --path .
```
   4. Validate the graph inputs:
```bash
codd validate --path .
```
   5. If validation passes, continue the normal operating cycle:
```bash
codd scan --path .
codd impact --path .
```

   ### Brownfield (have existing code, no requirements)

   1. Extract code structure:
```bash
codd extract
```
   2. Generate wave_config from extracted docs (`plan --init` auto-detects brownfield):
```bash
codd plan --init
```
   3. Restore design docs from extracted facts (use `/codd-restore` skill or CLI):
```bash
codd restore --wave 0 --path .   # Infer requirements from code
codd restore --wave 2 --path .   # Reconstruct system design
```
   4. Scan and validate:
```bash
codd scan --path .
codd validate --path .
```
   5. Continue with the normal operating cycle:
```bash
codd impact --path .
```

8. Report back with:
   - Which language option was used
   - Whether init created the skeleton successfully
   - Which documents were prepared for the first scan
   - Whether `codd validate --path .` passed or which issues remain
