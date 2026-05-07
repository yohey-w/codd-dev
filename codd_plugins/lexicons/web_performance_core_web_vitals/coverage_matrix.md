# Core Web Vitals Coverage Matrix

Source: web.dev Web Vitals and W3C Web Performance specifications.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `Largest Contentful Paint (LCP)` | Main content render time | LCP target, candidate content, measurement context, and threshold are explicit. | Main content loading is important but no LCP expectation or candidate is stated. |
| `Interaction to Next Paint (INP)` | Field interaction responsiveness | Interaction timing, input delay, processing time, presentation delay, or threshold is specified. | Interactive flows exist without a next-paint responsiveness expectation. |
| `Cumulative Layout Shift (CLS)` | Visual stability | Layout-shift sources, aggregation, and stability threshold are specified. | Late content, dynamic UI, or ads can move layout without a CLS expectation. |
| `Time to First Byte (TTFB)` | Navigation response latency | Request start, response start, server latency, or connection phases are covered. | Rendering metrics matter but server response timing is not constrained. |
| `First Contentful Paint (FCP)` | First visible content | First rendered content and threshold are specified for page load. | Users need feedback but no first visible content expectation exists. |
| `Time to Interactive (TTI)` | Lab load responsiveness | Quiet window, long task, network request, and interactive readiness expectations are stated. | A page may appear ready before controls can respond, but no lab readiness criterion exists. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.

