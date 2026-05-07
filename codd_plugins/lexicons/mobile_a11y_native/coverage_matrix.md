# Native Mobile Accessibility Coverage Matrix

Sources: W3C Mobile Accessibility, Apple Human Interface Guidelines
Accessibility and VoiceOver, Android accessibility guidance.

| Axis | Covered when | Implicit when | Gap when |
| --- | --- | --- | --- |
| `touchscreens` | Touch behavior, minimum target size, and target spacing are specified. | The feature exposes no touch-operable control and inherits a proven platform control. | A custom control or gesture is required but target size or touch behavior is absent. |
| `small screen sizes` | Small-screen layout, truncation, reachability, or device class behavior is specified. | The content is fixed to one non-mobile surface by requirement. | The feature is mobile-facing but compact layout behavior is unstated. |
| `different input modalities` | Speech, keyboard, switch, gesture alternative, or other input requirements are specified. | The artifact is read-only and has no interactive operation. | Core functionality depends on one input method with no stated alternative. |
| `color contrast` | Contrast ratios, non-color indicators, and state differentiation are specified. | Visual state is inherited wholly from an accessible platform component. | Text, icon, or state color is important but contrast or non-color signaling is absent. |
| `larger text sizes` | Dynamic Type, text enlargement, reflow, or large text behavior is specified. | The surface has no user-visible text. | Text can affect task completion but scaling behavior is unstated. |
| `screen reader` | Labels, roles, order, headings, announcements, and decorative exclusions are specified. | The content is plain system text with default semantic exposure. | Interactive or informative elements lack assistive technology semantics. |
| `haptics` | Haptic, visual, or text alternatives to audio/status feedback are specified. | The feature has no audio, transient, or status feedback. | Feedback is audio-only or tactile behavior is expected but unstated. |
| `Reduce Motion` | Reduced-motion alternatives, animation limits, or fade replacements are specified. | No animation, transition, or moving content is present. | Motion affects navigation, status, or feedback but accommodation is unstated. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`. Findings are
emitted only for `gap`.
