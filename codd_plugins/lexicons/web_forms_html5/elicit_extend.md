---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: web_forms_html5
observation_dimensions: 9
---

# HTML Forms Coverage Lexicon

Apply the base elicitation prompt, then inspect requirements and design notes
through the HTML forms axes declared in `lexicon.yaml`. Use HTML Living Standard
element, attribute, and state names as the dimension values. Do not infer a
form framework, validation library, or component system from these axes.

1. `input element`
2. `form element`
3. `client-side form validation`
4. `autocomplete`
5. `label element`
6. `fieldset`
7. `inputmode`
8. `form submission`
9. `File Upload state`

For every axis, classify coverage as:

- `covered`: the material names the HTML element, attribute, state, or API
  needed to judge the axis.
- `implicit`: the material points to a form contract, wire format, or design
  artifact that clearly covers the axis.
- `gap`: form behavior is required but the element, attribute, state, API, or
  user-facing rule is missing.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`; include `details.evidence` and a reviewer-facing
question when a form rule needs confirmation. Severity follows
`severity_rules.yaml`.

## Coverage-check examples

### covered

Requirement: "The checkout form uses `input type=email name=email required`,
`autocomplete=email`, and submits with `method=post` to `/checkout`."

Classification: `covered` for `input element`, `client-side form validation`,
`autocomplete`, `form element`, and `form submission`.

### implicit

Requirement: "Use the attached form contract for all contact fields and
submission payload names."

Classification: `implicit` for axes present in the attached contract when the
contract names the relevant HTML controls and attributes.

### gap

Requirement: "Users can upload supporting documents and submit the application."

Classification: `gap` for `File Upload state` if accepted file type and
multiplicity are unstated, and `gap` for `form submission` if action, method, or
payload names are absent.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
