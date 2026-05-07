---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: mobile_a11y_native
observation_dimensions: 8
---

# Native Mobile Accessibility Coverage Lexicon

Apply the base elicitation prompt, then inspect the material through the 8
native mobile accessibility axes declared in `lexicon.yaml`. Use the source
literals from W3C, Apple accessibility guidance, and Android accessibility
guidance. Do not infer a specific UI framework, platform toolkit, or app store
policy unless the requirement states it.

1. `touchscreens`
2. `small screen sizes`
3. `different input modalities`
4. `color contrast`
5. `larger text sizes`
6. `screen reader`
7. `haptics`
8. `Reduce Motion`

For every axis, classify coverage as:

- `covered`: the requirement explicitly states the mobile accessibility
  behavior, threshold, label, role, input alternative, feedback path, or motion
  accommodation that owns the axis.
- `implicit`: the described feature has no applicable behavior for the axis, or
  the material explicitly delegates the behavior to a platform component that
  provides the relevant accessibility semantics.
- `gap`: the material exposes user-facing mobile behavior but omits the
  accessibility detail needed to judge the axis.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing accessibility behavior requires human
confirmation. Severity follows `severity_rules.yaml`.

## Coverage-check examples

### covered

Requirement: "The settings screen uses 48dp minimum tap targets, supports
Dynamic Type, provides VoiceOver and TalkBack labels, and replaces motion
transitions with fades when Reduce Motion is enabled."

Classification: `covered` for `touchscreens`, `larger text sizes`,
`screen reader`, and `Reduce Motion`.

### implicit

Requirement: "The terms screen displays static platform text with no animation,
audio, gesture-only action, or custom controls."

Classification: `implicit` for `touchscreens`, `haptics`, and `Reduce Motion`
when the text semantics and scaling are inherited from the platform text
component.

### gap

Requirement: "Users swipe a color-coded card to archive an item and hear a
success sound."

Classification: `gap` for `different input modalities` if no non-swipe
alternative is stated, `gap` for `color contrast` if the color meaning is not
also conveyed another way, and `gap` for `haptics` if the feedback has no
non-audio alternative.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
