---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: i18n_unicode_cldr
observation_dimensions: 9
---

# Internationalization Coverage Lexicon

Apply the base elicitation prompt, then inspect the material through these
Unicode CLDR, ICU, and BCP 47 internationalization dimensions:

1. `locale_tagging`
2. `character_encoding`
3. `time_zone_handling`
4. `number_currency_format`
5. `date_time_calendar`
6. `text_collation`
7. `rtl_bidi_support`
8. `pluralization_rules`
9. `translation_string_management`

For each dimension, classify coverage as:

- `covered`: the requirement explicitly states the locale-aware behavior,
  accepted standard, or target-locales policy needed to judge the dimension.
- `implicit`: the dimension is not independently relevant because the material
  explicitly limits scope to a locale-neutral surface or references a shared
  internationalization baseline.
- `gap`: target locales, localized content, user-visible formatting, text input,
  or translated messages can be affected and no expectation is specified.

Emit findings only for dimensions classified as `gap`. Include the dimension in
`details.dimension`, the evidence or omission signal in `details.evidence`, and
a reviewer-facing question in `question`.

## Coverage-check examples

- `covered`: The requirement says all user-selected locales use BCP 47 tags and
  ICU date, number, currency, and plural formatting; classify the related axes
  as `covered`.
- `implicit`: An internal diagnostic endpoint returns only machine-readable UTC
  timestamps and no localized UI; classify display-only locale axes as
  `implicit`.
- `gap`: A multilingual signup flow includes translated labels and count-based
  validation messages but does not define plural categories or fallback strings;
  classify `pluralization_rules` and `translation_string_management` as `gap`.

Use recommended kinds as guidance. Do not invent dimensions outside the nine
listed above.
