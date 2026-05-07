---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: web_a11y_wcag22_aa
observation_dimensions: 13
---

# WCAG 2.2 AA Observation Dimensions

Apply the base elicitation prompt, then inspect the material through the 13 WCAG
2.2 guidelines. Treat Level A and Level AA success criteria as the required
coverage surface. Level AAA variants may appear only as informative context.

1. `1.1 Text Alternatives`
2. `1.2 Time-based Media`
3. `1.3 Adaptable`
4. `1.4 Distinguishable`
5. `2.1 Keyboard Accessible`
6. `2.2 Enough Time`
7. `2.3 Seizures and Physical Reactions`
8. `2.4 Navigable`
9. `2.5 Input Modalities`
10. `3.1 Readable`
11. `3.2 Predictable`
12. `3.3 Input Assistance`
13. `4.1 Compatible`

For each dimension, classify coverage as:

- `covered`: requirements explicitly state expectations for the guideline or
  its applicable A/AA success criteria.
- `implicit`: the guideline is not applicable and the non-applicability is clear
  from the material.
- `gap`: the guideline can affect user access and no expectation is specified.

Emit findings only for dimensions classified as `gap`. Include the dimension in
`details.dimension`, the evidence or omission signal in `details.evidence`, and
a reviewer-facing question in `question`.

## Coverage-check examples

- `covered`: A form requirement states that each field has a visible label,
  programmatic name, inline error message, and error summary; classify
  `3.3 Input Assistance` as `covered`.
- `implicit`: A static legal text page contains no video, audio, animation, or
  time-based media; classify `1.2 Time-based Media` as `implicit`.
- `gap`: A custom dropdown is required but there is no keyboard interaction,
  focus order, name, role, or value requirement; classify
  `2.1 Keyboard Accessible` and `4.1 Compatible` as `gap`.

Use recommended kinds as guidance. Do not invent additional dimensions outside
the thirteen listed above.
