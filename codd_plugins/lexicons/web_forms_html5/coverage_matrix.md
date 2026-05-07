# HTML Forms Coverage Matrix

Source: HTML Living Standard, Forms and input element sections.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `input element` | Typed controls | Required controls and their `type` states are explicit. | A form asks for user data without naming the controls or states. |
| `form element` | Form owner and transport | `form`, `action`, `method`, `enctype`, and submitted names are explicit where server communication matters. | Submission is expected but endpoint, method, encoding, or names are absent. |
| `client-side form validation` | Browser validation | Constraints such as `required`, `maxlength`, `pattern`, `min`, `max`, or `step` are declared. | Valid input rules exist only in prose or server-only behavior. |
| `autocomplete` | Autofill semantics | `autocomplete` or autofill field names are declared for repeat user data. | User profile fields omit autofill intent. |
| `label element` | Accessible captions | `label`, `for`, nested labeled controls, or labelable elements are specified. | Controls appear without captions or associations. |
| `fieldset` | Grouped controls | Related controls use `fieldset` and `legend` or equivalent declared grouping. | Radio or checkbox groups lack a group title. |
| `inputmode` | Mobile input modality | `inputmode` or modality expectations are stated for mobile entry. | Mobile numeric, email, URL, or telephone entry lacks input modality guidance. |
| `form submission` | Submit behavior | Submit controls and submitter overrides are declared where needed. | Buttons or flows imply submission without submit semantics. |
| `File Upload state` | File selection | `type=file`, `accept`, `multiple`, or `FileList` behavior is explicit. | Uploads are required but file selection constraints are absent. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
