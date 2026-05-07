# WebAuthn Coverage Matrix

Source: W3C Web Authentication Level 3.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `registration ceremony` | New public key credential creation | Creation options, relying party, user, challenge, authenticator selection, and make-credential behavior are explicit. | Enrollment is required but create() options or authenticator creation behavior are not specified. |
| `authentication ceremony` | Existing credential assertion | Request options, allowCredentials or discoverable behavior, challenge, get-assertion behavior, and response verification are explicit. | Sign-in requires WebAuthn but assertion inputs or verification responsibilities are omitted. |
| `attestation` | Authenticator property proof | Attestation conveyance, attestation object, statement format, and trust handling are specified or explicitly out of scope. | Registration depends on authenticator assurance but attestation treatment is unstated. |
| `user verification` | Local user verification assurance | `userVerification` requirement and availability behavior are stated for relevant ceremonies. | Assurance level matters but required/preferred/discouraged behavior is missing. |
| `credential management` | Credential lifecycle and storage | PublicKeyCredential lifecycle, discoverable credential policy, residentKey, store, and signal methods are covered as applicable. | Credentials can change, disappear, or sync but lifecycle behavior is omitted. |
| `extensions` | Client and authenticator extension processing | Extension identifiers, input/output dictionaries, and processing responsibilities are explicit. | Optional extension behavior is requested or implied without processing or result handling. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.

