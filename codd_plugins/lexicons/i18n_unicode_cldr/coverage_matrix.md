# Internationalization Coverage Matrix

Sources: Unicode CLDR 44, IETF BCP 47, and ICU internationalization guidance.

| Axis | Covered when | Implicit when | Gap when |
| --- | --- | --- | --- |
| `locale_tagging` | Supported locales use BCP 47 tags, script or region needs, Unicode extensions, and fallback policy. | The feature is explicitly locale-neutral or inherits a named i18n baseline. | Languages or regions are named but locale identifiers and fallback are absent. |
| `character_encoding` | UTF-8, Unicode normalization, and text-boundary behavior are specified for storage, APIs, and UI. | The system only handles opaque binary identifiers and no user-visible text. | Text input, import, export, search, or display exists without encoding or normalization expectations. |
| `time_zone_handling` | IANA time zones, DST transitions, UTC storage, and user or tenant zone selection are stated. | All timestamps are machine-only UTC values with no localized display. | Scheduling, deadlines, audit logs, or reminders exist without time zone behavior. |
| `number_currency_format` | Locale-aware numbers, currency codes, precision, rounding, and numbering systems are stated. | No user-visible quantities, money, measurements, or percentages are present. | Amounts or quantities are shown or entered without locale-aware formatting rules. |
| `date_time_calendar` | Date and time patterns, calendar system, week rules, and parsing expectations are specified. | Dates are internal ISO values and never exposed to users. | User-facing dates or calendars exist without locale-aware pattern or calendar behavior. |
| `text_collation` | Sort, search, comparison, case, accent, and numeric collation behavior are specified. | Lists are not sorted or searched by localized text. | Localized names, titles, or labels are sorted or searched without collation rules. |
| `rtl_bidi_support` | Direction, BiDi isolation, mirroring, and mixed-direction text behavior are specified. | Target locales explicitly exclude RTL scripts and no user-generated mixed text exists. | Arabic, Hebrew, Persian, Urdu, or mixed-direction content can appear without RTL/BiDi behavior. |
| `pluralization_rules` | CLDR cardinal and ordinal categories, including `zero`, `one`, `two`, `few`, `many`, and `other`, are covered. | No count-based natural-language messages are present. | Counted messages exist without locale-specific plural categories or fallback. |
| `translation_string_management` | Message catalogs, ICU MessageFormat, interpolation safety, review workflow, and fallback behavior are specified. | The product is explicitly single-language and no translation workflow exists. | Translated strings are needed but catalog, interpolation, fallback, or review process is unstated. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
