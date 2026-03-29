# CoDD Validate

Validate CoDD frontmatter and dependency references before impact analysis so broken metadata does not poison the graph or downstream change-propagation work.

## Usage

Run this skill after `codd scan --path .`, after editing requirement or design documents, or any time you suspect frontmatter or dependency references may be inconsistent.

## Instructions

1. Preflight:
   - Work from the project root.
   - Verify `codd/codd.yaml` exists. If not, run `codd init` first.
   - If the `codd` command is not available, install the package so the CLI entry point is on `PATH` before continuing.

2. Run validation:
```bash
codd validate --path .
```

3. Interpret the result:
   - PASS: Treat `OK: validated ...` output as a pass. Frontmatter is structurally valid enough to proceed, and dependency references are consistent enough for the next step. After a clean pass, `codd impact` is safe to run.
   - ERROR: Stop and fix the reported documents before continuing. Use the categories below to decide the repair.
   - BLOCKED or WARNING: Read the message, fix what is actionable, and re-run validation until the remaining output is understood.

4. ERROR triage by category:
   - `missing_field`: Required frontmatter data is missing or malformed. Open the affected design document and add or repair the needed fields, especially `node_id`, `title`, and `depends_on` when the design requires declared upstream dependencies. In current validator output, this often appears as missing or invalid frontmatter rather than a literal `missing_field` code.
   - `invalid_reference`: A document references a `node_id` that does not exist or is misspelled. Correct the typo or point the reference at the real upstream node. In current validator output, this usually shows up as undefined-reference messages such as dangling `depends_on` or `depended_by`.
   - `circular_dependency`: Two or more documents depend on each other in a loop. Revisit `depends_on`, remove the back-edge, and restore a one-way dependency order.

5. Fix broken frontmatter step by step:
   1. Open the document named in the validation error.
   2. Inspect the YAML frontmatter at the top of the file, bounded by `---`.
   3. Confirm the required fields are present and valid: `node_id`, `title`, and `depends_on` where applicable.
   4. Save the correction.
   5. Re-run `codd validate --path .` and confirm the error is gone.

6. Recommended `scan -> validate` flow:
```bash
codd scan --path .
codd validate --path .
# fix reported errors
codd validate --path .
```

7. Troubleshooting:
   - `Error: codd/ not found. Run 'codd init' first.`: Initialize the project before validating.
   - `command not found: codd`: Install the package or activate the environment that exposes the `codd` console script.
   - `invalid_frontmatter`: Fix YAML syntax first. Common causes are missing `:` separators, bad indentation, or an unclosed list entry.
   - `missing_frontmatter` or `missing_field`: Add the missing CoDD frontmatter block and required fields, then validate again.
   - `dangling_depends_on` or `dangling_depended_by`: Treat these as `invalid_reference` problems and repair the referenced `node_id`.
   - Repeated `circular_dependency` errors: Draw the dependency chain on paper, decide which document is the real upstream source of truth, and remove the reverse dependency.

8. Exit condition:
   - Do not move on to `codd impact` until validation is clean or the remaining warnings are explicitly understood and accepted.
