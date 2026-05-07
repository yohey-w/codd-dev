# Web Responsive Coverage Matrix

Source: MDN Using media queries, MDN current.

| Axis | Covered when | Implicit when | Gap when |
| --- | --- | --- | --- |
| `width` | Requirements or tests name `width`, `min-width`, `max-width`, or viewport breakpoint ranges. | Layout uses fluid CSS and only non-critical copy changes across widths. | No narrow, medium, or wide viewport behavior is specified. |
| `orientation` | Portrait and landscape behavior or non-dependence is stated. | The UI is single-column and no orientation-specific affordance exists. | Orientation could affect layout but is not addressed. |
| `prefers-color-scheme` | Light and dark scheme behavior is specified or explicitly out of scope. | Only system colors are used and no theme toggle exists. | Theme-specific color, asset, or contrast behavior is unclear. |
| `prefers-reduced-motion` | Motion reduction behavior is specified for animations or transitions. | No motion, animation, autoplay, or transition behavior exists. | Motion exists but reduced-motion behavior is not described. |
| `resolution` | High-density or low-density asset/rendering behavior is specified. | The UI uses vector assets or resolution-independent rendering only. | Raster images, canvas, maps, or charts exist without density expectations. |
| `hover` | Hover and non-hover interaction paths are both specified. | Hover is purely decorative and all actions are otherwise visible. | Required information or controls appear only on hover. |
| `pointer` | Fine, coarse, or no-pointer operation is specified for controls. | The UI uses platform-native controls with clear minimum sizes. | Pointer size or accuracy assumptions are unstated. |
| `aspect-ratio` | Wide, narrow, or constrained aspect-ratio behavior is specified. | Only fixed content with no viewport-dependent layout exists. | The layout may be embedded, split, or full-screen but aspect ratio is unstated. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
