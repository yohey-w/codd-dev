---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: web_security_owasp
observation_dimensions: 14
---

# OWASP Web Security Coverage Lexicon

Apply the base elicitation prompt, then inspect the source material through the
14 security axes declared in `lexicon.yaml`. The first 10 axes are the OWASP Top
10:2021 list. The remaining 4 axes are ASVS 4.0.3 verification areas used to
separate authentication, session, input validation, and output encoding detail
that is often too coarse in high-level requirements.

For every axis, classify coverage as:

- `covered`: the requirement explicitly states the control expectation and the
  protected trust boundary.
- `implicit`: the requirement implies enough security behavior to proceed
  because another explicit requirement or accepted design control covers the
  same boundary.
- `gap`: the material omits a security control that is required to judge the
  axis.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing control requires human confirmation.
Severity follows `severity_rules.yaml`; default security gaps are `high` or
`critical`, weak implicit evidence is `medium`, and residual notes are `info`.

## Coverage-check examples

### covered

Requirement: "All account update endpoints must enforce server-side role checks,
reject cross-tenant object identifiers, and log denied access attempts."

Classification: `covered` for `broken_access_control` because the requirement
states the server-side enforcement boundary, object access behavior, and denial
logging.

### implicit

Requirement: "Authenticated sessions use secure cookies from the shared platform
session module, and the platform security baseline applies to all services."

Classification: `implicit` for `session_management` when the referenced shared
baseline is present in the same material and defines token expiry, secure cookie
attributes, and invalidation behavior.

### gap

Requirement: "The import screen accepts a callback URL and the service fetches
the provided resource."

Classification: `gap` for `server_side_request_forgery_ssrf` when the material
does not specify allowed protocols, allowed destinations, network egress
boundaries, or metadata endpoint protections.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
