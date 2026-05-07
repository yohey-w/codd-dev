# WCAG 2.2 AA Coverage Matrix

Source: W3C WCAG 2.2 Recommendation, 2024-12-12.

| Axis | Covered when | Implicit when | Gap when |
| --- | --- | --- | --- |
| `1.1 Text Alternatives` | Non-text content alternatives are specified or no non-text content is present. | Decorative assets are explicitly decorative and ignored by assistive technology. | Images, icons, charts, media, or controls lack alternative text expectations. |
| `1.2 Time-based Media` | Captions, audio description, or media alternatives are specified for relevant media. | No prerecorded or live audio/video exists. | Audio or video exists without A/AA alternative expectations. |
| `1.3 Adaptable` | Structure, relationships, sequence, orientation, and input purpose are specified. | Simple prose content has semantic HTML defaults and no custom structure. | Visual structure or ordering exists without programmatic expectations. |
| `1.4 Distinguishable` | Color, contrast, resizing, reflow, text spacing, and hover/focus content are specified. | Styling is inherited from a verified design system with named a11y guarantees. | Visual presentation affects comprehension but no contrast or reflow criteria exist. |
| `2.1 Keyboard Accessible` | Keyboard operation and trap prevention are specified for all functionality. | The requirement covers static content with no controls. | Interactive controls exist without keyboard behavior. |
| `2.2 Enough Time` | Time limits, auto-updating content, or motion controls are specified. | There are no time limits, auto-play, or auto-updating regions. | Timers, sessions, or moving content exist without controls or limits. |
| `2.3 Seizures and Physical Reactions` | Flash thresholds or no-flash constraints are specified. | No flashing or rapid visual changes exist. | Animation or flashing exists without threshold expectations. |
| `2.4 Navigable` | Page titles, headings, focus order, bypass, link purpose, and focus visibility are specified. | A single static page has clear native structure and no repeated blocks. | Navigation or focus path is relevant but unspecified. |
| `2.5 Input Modalities` | Pointer gestures, cancellation, label in name, dragging, and target size are specified. | There are no custom pointer or touch interactions. | Touch, dragging, or pointer interactions lack accessible alternatives. |
| `3.1 Readable` | Page and part languages are specified. | Single-language content inherits a documented app-wide language. | Multilingual or localized content lacks language expectations. |
| `3.2 Predictable` | Focus, input, navigation, identification, and help consistency are specified. | A static page has no focus-triggered or input-triggered changes. | Dynamic behavior can surprise users and is not constrained. |
| `3.3 Input Assistance` | Error identification, labels, prevention, redundant entry, and authentication support are specified. | There are no forms, authentication, or user-entered data. | Forms or auth exist without labels, errors, prevention, or cognitive support. |
| `4.1 Compatible` | Name, role, value, and status message semantics are specified. | Only native semantic elements are used with no dynamic state. | Custom controls, live regions, or state changes lack assistive technology semantics. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
