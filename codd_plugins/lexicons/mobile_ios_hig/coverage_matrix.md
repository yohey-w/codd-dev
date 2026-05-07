# Apple HIG Mobile Coverage Matrix

Source: Apple Human Interface Guidelines, current.

| Axis | Covered when | Implicit when | Gap when |
| --- | --- | --- | --- |
| `Navigation and search` | Navigation model, search entry, and empty/no-result behavior are stated. | The surface is a single modal or single-purpose flow with no information hierarchy. | People must move among screens or locate content, but the route or search behavior is unstated. |
| `Typography` | Text hierarchy, scaling, truncation, and legibility expectations are specified. | The artifact has no user-visible text beyond platform-provided labels. | Text density, hierarchy, or scaling can affect comprehension but is not specified. |
| `Color` | Color roles, semantic usage, and light/dark behavior are specified. | Color is inherited from a system component without custom roles. | Color carries meaning or brand but no state or contrast expectation is stated. |
| `Accessibility` | Assistive technology, dynamic text, contrast, captions, or accommodation behavior is explicit. | The item is non-interactive and fully described elsewhere. | Any user-visible or interactive behavior exists without accessibility expectations. |
| `Playing haptics` | Haptic feedback purpose, timing, and fallback are specified. | The experience does not use tactile feedback. | Haptic feedback is implied by success, error, selection, or gesture behavior but not described. |
| `Motion` | Motion purpose, transition behavior, and reduced-motion handling are specified. | There is no animation, transition, autoplay, or motion response. | Motion exists or is likely in the interaction but no motion expectation is stated. |
| `Inputs` | Touch, keyboard, pointer, gesture, and alternative input paths are specified. | The content is read-only and has no controls. | A control or gesture exists without input method or alternative behavior. |
| `Layout` | Safe areas, orientation, size class, and adaptive layout behavior are specified. | The surface is fixed by a platform container and has no custom layout. | Layout may change by device or context but no adaptive behavior is stated. |
| `Icons` | Icon meaning, labeling, selected state, and asset expectations are specified. | Icons are decorative and redundant with text. | Icon-only controls or custom symbols are present without meaning or label coverage. |
| `Playing audio` | Playback, mute, interruption, and user control behavior are specified. | The feature has no audio. | Audio can play or record but behavior is not stated. |
| `Privacy` | Data collection, permission request, disclosure, and denial behavior are specified. | The feature uses no personal data, sensors, account, or network data. | Data, sensor, or account access is required but privacy behavior is missing. |
| `Feedback` | Status, confirmation, alerts, destructive actions, and error recovery are specified. | The operation has no asynchronous, destructive, or failure state. | A user action can succeed, fail, or destroy data but feedback is unspecified. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`. Findings are
emitted only for `gap`.
