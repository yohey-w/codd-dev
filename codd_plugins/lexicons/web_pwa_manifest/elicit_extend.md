---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: web_pwa_manifest
observation_dimensions: 8
---

# Web App Manifest Coverage Lexicon

Apply the base elicitation prompt, then inspect installability and app-launch
requirements through the manifest axes declared in `lexicon.yaml`. Use Web App
Manifest and Web Share Target member names as dimension values. Do not infer a
specific application shell, service worker strategy, or build tool.

1. `manifest members`
2. `icons`
3. `display`
4. `start_url`
5. `scope`
6. `theme_color`
7. `shortcuts`
8. `share_target`

For every axis, classify coverage as:

- `covered`: the material names the manifest member, value, or payload mapping
  needed to judge the axis.
- `implicit`: the material references a webmanifest or installability contract
  that clearly contains the member.
- `gap`: install, launch, navigation, presentation, shortcut, or share behavior
  is required but the manifest member is missing.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`; include `details.evidence` and ask the reviewer
to confirm manifest member values when absent.

## Coverage-check examples

### covered

Requirement: "The PWA manifest sets `name`, `icons` with maskable purpose,
`display=standalone`, `start_url=/app`, `scope=/app/`, and
`theme_color=#ffffff`."

Classification: `covered` for `manifest members`, `icons`, `display`,
`start_url`, `scope`, and `theme_color`.

### implicit

Requirement: "Use the attached production `manifest.webmanifest` for app
installation metadata."

Classification: `implicit` for axes that are present in that manifest.

### gap

Requirement: "Installed users can open the app directly from the share sheet
and jump to Compose from the app icon."

Classification: `gap` for `share_target` if action and params are absent, and
`gap` for `shortcuts` if the quick action URL is unstated.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
