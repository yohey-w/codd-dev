---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: web_authn_webauthn
observation_dimensions: 6
---

# WebAuthn Coverage Lexicon

Apply the base elicitation prompt, then inspect the material through the six
WebAuthn axes declared in `lexicon.yaml`. Use Web Authentication Level 3 terms
for ceremonies, public key credentials, attestation, verification, lifecycle,
and extensions. Do not infer authenticator product behavior from vendor names.

1. `registration ceremony`
2. `authentication ceremony`
3. `attestation`
4. `user verification`
5. `credential management`
6. `extensions`

For every axis, classify coverage as:

- `covered`: the requirement explicitly states the ceremony, option dictionary,
  authenticator operation, verification requirement, credential lifecycle action,
  or extension processing requirement needed to judge the axis.
- `implicit`: the material refers to an attached WebAuthn profile, security
  architecture, or relying-party policy that is present and clearly covers the
  axis.
- `gap`: WebAuthn behavior is required but the material omits the option,
  response, assurance, lifecycle, or extension detail needed to review it.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when ceremony or security policy details require human
confirmation. Severity follows `severity_rules.yaml`; registration,
authentication, and user verification gaps are usually `critical`, while
attestation and credential-management gaps are usually `high`.

## Coverage-check examples

### covered

Requirement: "Registration calls `navigator.credentials.create()` with
`PublicKeyCredentialCreationOptions`, sets `userVerification` to `required`,
and verifies the returned `attestation object` before storing a
`PublicKeyCredential` record."

Classification: `covered` for `registration ceremony`, `user verification`,
`attestation`, and `credential management`.

### implicit

Requirement: "The attached relying-party security profile is authoritative for
all WebAuthn ceremonies and defines credential creation, assertion, and
discoverable credential behavior."

Classification: `implicit` for `registration ceremony`, `authentication
ceremony`, and `credential management` when that profile is present and includes
the ceremony options.

### gap

Requirement: "Users sign in with passkeys and can recover access on a new
device."

Classification: `gap` for `authentication ceremony` if
`PublicKeyCredentialRequestOptions` or credential selection are not stated, and
`gap` for `credential management` if discoverable credential and lifecycle
synchronization behavior are not described.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.

