---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: web_browser_compat
observation_dimensions: 7
---

# Browser Compatibility Baseline Coverage Lexicon

Apply the base elicitation prompt, then inspect material through the Baseline
compatibility axes declared in `lexicon.yaml`. Use Baseline status and support
threshold language. Do not infer a browser, build tool, or package from these
axes.

1. `Newly available`
2. `Widely available`
3. `Limited availability`
4. `core browser set`
5. `Baseline threshold`
6. `polyfill`
7. `progressive enhancement`

For every axis, classify coverage as:

- `covered`: the material states the Baseline status, support threshold,
  fallback, or impact category needed to judge compatibility.
- `implicit`: the material references a browser support matrix or release
  policy that contains the missing detail.
- `gap`: feature adoption depends on browser support but the status, audience,
  threshold, polyfill, or unsupported impact is missing.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`; include `details.evidence` and ask the reviewer
to confirm the compatibility threshold when unclear.

## Coverage-check examples

### covered

Requirement: "Use features in `Baseline 2024` or older. Any `Newly available`
feature must have user support data above 98% or be an `Enhancement` without a
polyfill."

Classification: `covered` for `Baseline threshold`, `Newly available`,
`progressive enhancement`, and `polyfill`.

### implicit

Requirement: "The attached browser-support policy is authoritative for all CSS
and Web API usage."

Classification: `implicit` for axes present in that policy when it includes
Baseline statuses and fallback rules.

### gap

Requirement: "Use the newest CSS feature and add a compatibility fallback if
needed."

Classification: `gap` for `Limited availability` if status is unknown, `gap`
for `Baseline threshold` if the target is unstated, and `gap` for `polyfill` if
the fallback cost is not decided.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
