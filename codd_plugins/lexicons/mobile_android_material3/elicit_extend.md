---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: mobile_android_material3
observation_dimensions: 12
---

# Material Design 3 Mobile Observation Dimensions

Apply the base elicitation prompt, then inspect the material through these
Material Design 3 dimensions:

1. `Color`
2. `Typography`
3. `Shape`
4. `Elevation`
5. `Motion`
6. `Components`
7. `Icons`
8. `Accessibility`
9. `Adaptive design`
10. `Interaction`
11. `Content design`
12. `Dynamic color`

For each dimension, classify coverage as:

- `covered`: requirements explicitly state the expected behavior for this
  dimension.
- `implicit`: the dimension is not independently relevant because the described
  feature does not expose that behavior or delegates it entirely to a platform
  default.
- `gap`: the dimension can affect user-visible behavior and no expectation is
  specified.

Emit findings only for dimensions classified as `gap`. Include the dimension in
`details.dimension`, the evidence or omission signal in `details.evidence`, and
a reviewer-facing question in `question`.

## Coverage-check examples

- `covered`: The requirement names a primary container, disabled state, error
  state, and all text roles; classify `Components`, `Interaction`, `Color`, and
  `Typography` as `covered`.
- `implicit`: A read-only legal disclosure uses no custom icon, motion, or
  dynamic color behavior; classify `Icons`, `Motion`, and `Dynamic color` as
  `implicit`.
- `gap`: A dashboard must work on compact and expanded screens, but no pane,
  breakpoint, or layout behavior is described; classify `Adaptive design` as
  `gap`.

Use recommended kinds as guidance. Do not invent additional dimensions outside
the twelve listed above.
