---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: compliance_pci_dss_4
observation_dimensions: 12
---

# PCI DSS v4.0 Compliance Observation Dimensions

Apply the base elicitation prompt, then inspect requirements and design notes
through these PCI DSS v4.0 payment account data security dimensions:

1. `Install and Maintain Network Security Controls`
2. `Protect Stored Account Data`
3. `Maintain a Vulnerability Management Program`
4. `Restrict Access to System Components and Cardholder Data by Business Need to Know`
5. `Regularly Monitor and Test Networks`
6. `Support Information Security with Organizational Policies and Programs`
7. `Protect Cardholder Data with Strong Cryptography During Transmission Over Open, Public Networks`
8. `Develop and Maintain Secure Systems and Software`
9. `Personnel screening`
10. `Restrict Physical Access to Cardholder Data`
11. `Security incident response plan`
12. `PCI DSS scope`

For each dimension, classify coverage as:

- `covered`: requirements explicitly state the PCI DSS control, workflow,
  non-applicability, scoping decision, or assessed third-party responsibility.
- `implicit`: the material makes the dimension irrelevant, such as no account
  data, no CDE impact, no storage, no open-public-network transmission, or a
  documented PCI-assessed third party owns the control completely.
- `gap`: account data, a cardholder data environment, payment functionality, or
  a system that can affect CDE security exists and the dimension can affect
  protection, detection, assessment, response, or auditability.

Emit findings only for dimensions classified as `gap`. Include the dimension in
`details.dimension`, the payment flow or omission in `details.evidence`, and a
reviewer-facing question in `question`.

## Coverage-check examples

- `covered`: An e-commerce design documents PCI DSS scope, cardholder data
  flows, outsourced payment responsibility, network controls, no local PAN
  storage, strong cryptography, access roles, logging, vulnerability scanning,
  incident response, and third-party monitoring; classify those dimensions as
  `covered`.
- `implicit`: A SaaS billing page redirects entirely to a PCI-assessed hosted
  payment page, stores no account data, and cannot affect payment page scripts
  or CDE systems; classify storage, network, physical access, and local secure
  software dimensions as `implicit`.
- `gap`: A checkout design embeds payment scripts and stores partial cardholder
  records but does not define PCI DSS scope, script controls, cryptography,
  monitoring, or incident response; classify `PCI DSS scope`, `Develop and
  Maintain Secure Systems and Software`, `Protect Cardholder Data with Strong
  Cryptography During Transmission Over Open, Public Networks`, `Regularly
  Monitor and Test Networks`, and `Security incident response plan` as `gap`.

Use recommended kinds as guidance. Do not invent dimensions outside the twelve
listed above.
