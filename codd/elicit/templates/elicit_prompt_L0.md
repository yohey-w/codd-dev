# Elicitation Prompt L0

You are reviewing requirements and design notes to discover missing context.

Use only the supplied project material. Do not invent facts. When evidence is
missing, ask a precise question or record the uncertainty as a finding.

## Mode selection

- **No lexicon loaded** → "discover" mode: surface ~10 high-signal findings about
  unclear, missing, or ambiguous specification items. Backwards compatible.
- **Lexicon loaded** → "coverage-check" mode: walk the lexicon's category list
  and emit findings ONLY for categories classified as `gap`. Do not invent
  findings outside the lexicon's scope. Items merely implied by the requirements
  count as `implicit` and produce no finding.

## Coverage-check procedure (lexicon-loaded mode)

1. Identify the lexicon-defined category list from the loaded lexicon section.
2. For each category, mark it as one of:
   - `covered`: requirement explicitly addresses the category
   - `implicit`: requirement implies the category (judgment call, no finding)
   - `gap`: requirement does not address the category
3. Emit a finding ONLY for `gap` categories.
4. Populate `lexicon_coverage_report` with the full mapping
   (`{category_label: "covered" | "implicit" | "gap"}`).
5. Set `all_covered` to `true` only when every category is `covered` or
   `implicit` and findings is empty. Otherwise set `all_covered` to `false`.

For project-specific items that fall outside the loaded lexicon scope, mark
`severity` as `info` and add `details.note: "outside_lexicon_scope"`. Do not
fabricate categories that the lexicon did not enumerate.

## Inputs

- Requirements: `{{requirements_content}}`
- Design documents: `{{design_doc_content}}`
- Project lexicon: `{{project_lexicon}}`
- Existing coverage axes: `{{existing_axes}}`

## Output

Return a JSON object with the following shape:

```json
{
  "all_covered": <boolean>,
  "lexicon_coverage_report": { "<category>": "covered" | "implicit" | "gap" },
  "findings": [
    {
      "id": "<stable snake_case>",
      "kind": "<concise category for this finding>",
      "severity": "critical|high|medium|info",
      "name": "<short summary>",
      "question": "<question for a human reviewer, when useful>",
      "details": { "<evidence keys>": "<values>" },
      "related_requirement_ids": ["<requirement IDs>"],
      "rationale": "<why the finding matters>"
    }
  ]
}
```

Return only the JSON object, with no prose before or after it. When no lexicon
is loaded, you may return a JSON array of findings instead (legacy "discover"
mode is preserved for backwards compatibility).
