# Browser Compatibility Baseline Coverage Matrix

Source: web.dev Baseline, MDN Baseline compatibility, and web.dev Baseline
polyfill guidance.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `Newly available` | Newly interoperable feature status | Feature use identifies newly available status and target support implication. | A recent web feature is used without status or audience support evidence. |
| `Widely available` | Mature baseline status | Feature use identifies widely available status or a safe baseline target. | Broad support is assumed but not tied to Baseline evidence. |
| `Limited availability` | Non-baseline support risk | Limited availability is called out with fallback, avoidance, or risk acceptance. | Unsupported browser behavior is possible but not handled. |
| `core browser set` | Browser population | The compatibility statement names the Baseline browser-set boundary or equivalent target audience. | Compatibility is claimed without a support population. |
| `Baseline threshold` | Project target | A Baseline year, user support percentage, or threshold policy is declared. | Teams cannot decide whether a feature is acceptable for production. |
| `polyfill` | Fallback strategy | Polyfill need and cost are explicitly accepted or rejected. | Polyfills are added or omitted without support and cost rationale. |
| `progressive enhancement` | Unsupported impact | Enhancement, additive, or critical impact is classified with fallback behavior. | Limited support features may fail without an impact category. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
