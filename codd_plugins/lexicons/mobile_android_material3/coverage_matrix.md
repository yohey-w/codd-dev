# Material Design 3 Mobile Coverage Matrix

Source: Material Design 3, current.

| Axis | Covered when | Implicit when | Gap when |
| --- | --- | --- | --- |
| `Color` | Color roles, schemes, semantic use, and state colors are specified. | The feature uses only default component colors with no custom semantic state. | Color communicates state, brand, or hierarchy but no role expectation is stated. |
| `Typography` | Type scale, text hierarchy, truncation, and token use are specified. | The surface has no user-visible text beyond inherited component labels. | Text can affect comprehension or hierarchy but typography is unstated. |
| `Shape` | Shape role, corner radius, and component shape variation are specified. | No custom surfaces, containers, or shaped affordances are present. | Shape communicates brand, grouping, or affordance but no expectation is stated. |
| `Elevation` | Surface hierarchy, shadow, tint, and elevation tokens are specified. | The UI is flat and has no overlapping or stacked surfaces. | Surfaces overlap or imply hierarchy but elevation is not described. |
| `Motion` | Transitions, easing, duration, and reduced-motion expectations are specified. | The feature has no animation, transition, or motion response. | Motion is likely in navigation, state change, or feedback but is unstated. |
| `Components` | Component type, variant, state, and accessibility behavior are specified. | The artifact is informational and uses no interactive UI building blocks. | Required interaction exists but component behavior is ambiguous. |
| `Icons` | Icon meaning, style, labeling, and state behavior are specified. | Icons are decorative and redundant with adjacent text. | Icon-only or custom icons are present without meaning or label coverage. |
| `Accessibility` | Assistive technology, touch target, contrast, text scaling, and semantics are specified. | The element is non-interactive and fully described by surrounding text. | Any interactive or informative element lacks accessibility expectations. |
| `Adaptive design` | Window size, layout pane, breakpoint, or adaptive behavior is specified. | The UI is constrained to a fixed component container with no custom layout. | The experience must run across screen sizes but no adaptive behavior is stated. |
| `Interaction` | Gestures, selection, focus, pressed, hovered, disabled, and dragged states are specified. | The surface has no controls or stateful elements. | Controls or state changes exist without state and gesture expectations. |
| `Content design` | Labels, helper text, error text, empty states, and voice/tone are specified. | No product-authored copy appears in the feature. | User-facing copy is required but no content rule or state copy is stated. |
| `Dynamic color` | Source, scheme, fallback, and personalization behavior are specified. | Dynamic color is explicitly out of scope and a static scheme is used. | Personalization or theme adaptation is expected but unspecified. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`. Findings are
emitted only for `gap`.
