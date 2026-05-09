# OWASP MASVS Mobile Security Coverage Matrix

Source: OWASP Mobile Application Security Verification Standard v2.x.

| Axis | Covered when | Implicit when | Gap or not_found when |
| --- | --- | --- | --- |
| `storage_security` | Sensitive data storage, retention, logs, backups, and cache behavior are stated. | The feature handles no sensitive data and persists no local state. | Mobile data can be stored locally, cached, backed up, or logged without controls. |
| `crypto_best_practices` | Cryptographic purpose, algorithms, key lifecycle, and platform crypto use are stated. | The feature performs no cryptographic operation and relies on stated platform defaults. | Encryption, signing, hashing, or key handling is implied but unspecified. |
| `auth_session_management` | Authentication, authorization, token/session lifecycle, logout, and recovery behavior are stated. | The feature is fully anonymous and has no identity or privileged state. | User identity, roles, sessions, or credentials are involved without explicit handling. |
| `network_communication` | TLS, certificate validation, endpoint trust, and cleartext prevention are stated. | The feature performs no network communication. | Remote communication is required but transport security is missing or ambiguous. |
| `platform_interaction` | Permissions, OS APIs, WebViews, IPC, sensors, and app-to-app channels are stated. | The feature uses no privileged OS API or external app interaction. | Platform capability, permission, or IPC behavior is required but unstated. |
| `code_quality` | Secure coding, build configuration, dependency hygiene, and unsafe API expectations are stated. | The artifact is non-executable policy or content and creates no implementation surface. | Implementation quality can affect security but no code or dependency baseline exists. |
| `resilience` | Reverse engineering, tamper resistance, runtime integrity, and abuse response are stated where needed. | The app has no sensitive business logic, secrets, or abuse-prone client trust assumptions. | Client-side trust, anti-tamper, or reverse-engineering risk matters but is unspecified. |

Reviewers classify each axis as `covered`, `implicit`, `gap`, or `not_found`.
Findings are emitted for `gap` and `not_found`.
