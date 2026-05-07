# Elicitation Prompt L0

You are reviewing requirements and design notes to discover missing context.

Use only the supplied project material. Do not invent facts. When evidence is
missing, ask a precise question or record the uncertainty as a finding.
Aim for about ten high-signal findings. Prefer fewer findings when the supplied
material does not justify more.

## Inputs

- Requirements: `{{requirements_content}}`
- Design documents: `{{design_doc_content}}`
- Project lexicon: `{{project_lexicon}}`
- Existing coverage axes: `{{existing_axes}}`

## Output

Return a JSON array. Each item must include:

- `id`: stable snake_case identifier
- `kind`: concise category chosen for this finding
- `severity`: one of `critical`, `high`, `medium`, `info`
- `name`: short summary
- `question`: question for a human reviewer, when useful
- `details`: object with supporting evidence
- `related_requirement_ids`: array of related requirement IDs
- `rationale`: why the finding matters

Return only the JSON array, with no prose before or after it.
