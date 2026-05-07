# Web App Manifest Coverage Matrix

Source: W3C Web Application Manifest and Web Share Target API extension.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `manifest members` | Core metadata | `name`, `short_name`, description, language, or direction choices are explicit as needed. | Installation or launch surfaces need names but metadata is absent. |
| `icons` | Application images | `icons` entries include source, sizes, type, and purpose where relevant. | App install or launch icons are required but unspecified. |
| `display` | Presentation mode | `display` mode and fallback expectation are declared. | Installed app presentation is unspecified. |
| `start_url` | Launch route | `start_url`, launch behavior, or application identity are explicit. | Installed app launch URL is unclear. |
| `scope` | URL boundary | `scope`, within-scope behavior, or deep-link handling is declared. | Off-scope navigation or deep links are possible but undefined. |
| `theme_color` | Visual metadata | `theme_color` and related color metadata are specified. | Installed UI colors are important but absent. |
| `shortcuts` | Quick actions | Shortcut names, URLs, and icons are declared when quick actions are required. | Quick actions are expected but no manifest shortcuts exist. |
| `share_target` | Shared data entry | `share_target`, action, method, encoding, and params are declared. | The app should receive shared data but manifest handling is absent. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
