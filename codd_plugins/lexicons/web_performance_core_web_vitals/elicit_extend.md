---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: web_performance_core_web_vitals
observation_dimensions: 6
---

# Core Web Vitals Coverage Lexicon

Apply the base elicitation prompt, then inspect the material through the six
web performance axes declared in `lexicon.yaml`. Use source metric names and
Web Performance API terminology. Do not infer tool-specific audit behavior from
product names or implementation dashboards.

1. `Largest Contentful Paint (LCP)`
2. `Interaction to Next Paint (INP)`
3. `Cumulative Layout Shift (CLS)`
4. `Time to First Byte (TTFB)`
5. `First Contentful Paint (FCP)`
6. `Time to Interactive (TTI)`

For every axis, classify coverage as:

- `covered`: the requirement explicitly states the metric, source event, timing
  phase, candidate content, or threshold required to judge the axis.
- `implicit`: the material refers to an attached performance budget, design
  contract, or measurement plan that is present and clearly covers the axis.
- `gap`: performance is user-relevant but the material omits the metric,
  timing phase, threshold, or evidence needed to judge the axis.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when a performance budget or measurement target needs
human confirmation. Severity follows `severity_rules.yaml`; Core Web Vitals
gaps are usually `critical`, while supporting metric gaps are usually `high` or
`medium`.

## Coverage-check examples

### covered

Requirement: "The product page must keep `Largest Contentful Paint (LCP)` at
`2.5 seconds or less`, report `Interaction to Next Paint (INP)` at `200
milliseconds or less`, and prevent `Cumulative Layout Shift (CLS)` above `0.1
or less` on mobile and desktop cohorts."

Classification: `covered` for `Largest Contentful Paint (LCP)`, `Interaction
to Next Paint (INP)`, and `Cumulative Layout Shift (CLS)`.

### implicit

Requirement: "The attached web performance budget is authoritative for page
load and interaction metrics and includes the production RUM measurement plan."

Classification: `implicit` for the metrics present in that attached budget when
the budget includes metric names and thresholds.

### gap

Requirement: "The dashboard should feel fast after login and avoid jumping
while data loads."

Classification: `gap` for `Largest Contentful Paint (LCP)` if main content
render timing is unstated, and `gap` for `Cumulative Layout Shift (CLS)` if
layout stability has no threshold or placeholder policy.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.

