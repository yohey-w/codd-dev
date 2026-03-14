# CoDD Init

Initialize CoDD (Coherence-Driven Development) in the current project.

## Usage

Run this skill to create a `codd/` directory with:
- Project config (`codd.yaml`)
- Annotation templates (conventions, doc_links, data_dependencies, overrides)
- SQLite graph database
- `.gitignore` for generated files

## Instructions

1. Ask the user for:
   - Project name (default: current directory name)
   - Primary language (python/typescript/java/go)

2. Run the CLI:
```bash
python -m codd.cli init --project-name "<name>" --language "<lang>" --dest "."
```

3. Guide the user to fill in initial annotations:
   - `codd/annotations/conventions.yaml` — implicit rules
   - `codd/annotations/doc_links.yaml` — requirement ⇔ code links
   - `codd/annotations/data_dependencies.yaml` — data-driven deps

4. Confirm the skeleton was created successfully.
