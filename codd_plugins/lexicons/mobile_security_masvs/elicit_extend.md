---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: mobile_security_masvs
observation_dimensions: 7
---

# OWASP MASVS Mobile Security Coverage Lexicon

Apply the base elicitation prompt, then inspect mobile application requirements
through these OWASP MASVS v2 coverage axes:

1. `storage_security`
2. `crypto_best_practices`
3. `auth_session_management`
4. `network_communication`
5. `platform_interaction`
6. `code_quality`
7. `resilience`

For each axis, classify coverage as:

- `covered`: requirements explicitly state the expected mobile security behavior
  for this axis.
- `implicit`: the axis is not independently relevant because the described
  feature does not use that capability or delegates it entirely to a platform
  control that is already stated.
- `gap`: the axis can affect mobile security and no expectation is specified.
- `not_found`: no evidence for the axis exists in the reviewed material when
  downstream scoring needs a missing-control status distinct from `gap`.

Emit findings only for `gap` or `not_found` axes. Include the axis in
`details.dimension`, the evidence or omission signal in `details.evidence`, and
a reviewer-facing question in `question`.

## Coverage-check examples

- `covered`: The requirement says access tokens are stored only in a platform
  secure storage facility, expire on logout, and are never logged; classify
  `storage_security` and `auth_session_management` as `covered`.
- `implicit`: A local calculator screen has no account, network, sensor, or
  persistent data behavior; classify `auth_session_management`,
  `network_communication`, and `platform_interaction` as `implicit`.
- `gap`: The app uploads profile images to an API, but TLS, certificate
  validation, and cleartext traffic behavior are unstated; classify
  `network_communication` as `gap`.
- `not_found`: The reviewed mobile specification contains no storage,
  cryptography, authentication, network, platform, code-quality, or resilience
  evidence; emit `not_found` findings for applicable release-blocking axes.

Use recommended kinds as guidance. Do not invent additional MASVS axes outside
the seven listed above.
