---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: web_seo_schemaorg
observation_dimensions: 8
---

# Schema.org SEO Coverage Lexicon

Apply the base elicitation prompt, then inspect requirements and content models
through the Schema.org axes declared in `lexicon.yaml`. Use Schema.org type
names as the dimension values. Do not infer search-engine product behavior or
ranking guarantees from these axes.

1. `Organization`
2. `Person`
3. `Product`
4. `Article`
5. `BreadcrumbList`
6. `FAQPage`
7. `Event`
8. `VideoObject`

For every axis, classify coverage as:

- `covered`: the material names the Schema.org type or subtype needed for the
  content entity.
- `implicit`: the material references a structured-data contract or JSON-LD
  template that clearly includes the type.
- `gap`: the entity is user- or machine-visible but the Schema.org type choice
  is absent.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`; include `details.evidence` and ask which
Schema.org type should be authoritative when several types could apply.

## Coverage-check examples

### covered

Requirement: "Product detail pages emit JSON-LD with `Product`, seller
`Organization`, and page breadcrumbs as `BreadcrumbList`."

Classification: `covered` for `Product`, `Organization`, and
`BreadcrumbList`.

### implicit

Requirement: "Use the attached structured-data template for all news pages."

Classification: `implicit` for `Article` when the template declares
`NewsArticle` and required supporting types.

### gap

Requirement: "The landing page shows company details, a founder biography, and
FAQ content."

Classification: `gap` for `Organization`, `Person`, and `FAQPage` if no
Schema.org type or JSON-LD contract is specified.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
