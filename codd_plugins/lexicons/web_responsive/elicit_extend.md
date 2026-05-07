---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: web_responsive
observation_dimensions: 8
---

# Web Responsive Observation Dimensions

Apply the base elicitation prompt, then inspect the material through these CSS
media query dimensions derived from MDN:

1. `width`
2. `orientation`
3. `prefers-color-scheme`
4. `prefers-reduced-motion`
5. `resolution`
6. `hover`
7. `pointer`
8. `aspect-ratio`

For each dimension, classify coverage as:

- `covered`: requirements explicitly state expectations for this media feature.
- `implicit`: the feature is not independently relevant because the described UI
  avoids that condition or uses a responsive-independent implementation.
- `gap`: the feature can affect user-visible behavior and no expectation is
  specified.

Emit findings only for dimensions classified as `gap`. Include the dimension in
`details.dimension`, the evidence or omission signal in `details.evidence`, and
a reviewer-facing question in `question`.

## Coverage-check examples

- `covered`: The requirement says the product grid becomes one column below
  `max-width: 640px` and three columns above `min-width: 1024px`; classify
  `width` as `covered`.
- `implicit`: A static receipt page has no animation, transition, or autoplay;
  classify `prefers-reduced-motion` as `implicit`.
- `gap`: A menu exposes required actions only in a hover flyout and has no
  touch or keyboard alternative; classify `hover` as `gap`.

Use recommended kinds as guidance. Do not invent additional dimensions outside
the eight listed above.
