---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: twelve_factor_app
observation_dimensions: 12
---

# Twelve-Factor App Coverage Lexicon

Apply the base elicitation prompt, then inspect the material through these
Twelve-Factor App dimensions:

1. `codebase`
2. `dependencies`
3. `config`
4. `backing_services`
5. `build_release_run`
6. `processes`
7. `port_binding`
8. `concurrency`
9. `disposability`
10. `dev_prod_parity`
11. `logs`
12. `admin_processes`

For each dimension, classify coverage as:

- `covered`: the requirement explicitly states the cloud-native operating
  contract, deployment behavior, or implementation constraint for the factor.
- `implicit`: the factor is satisfied by a named platform baseline, deployment
  standard, or documented scope exclusion in the same material.
- `gap`: the app is delivered as a service and the material omits the factor in a
  way that could affect portability, deployability, scalability, or operations.

Emit findings only for dimensions classified as `gap`. Include the dimension in
`details.dimension`, the evidence or omission signal in `details.evidence`, and
a reviewer-facing question in `question`.

## Coverage-check examples

- `covered`: The deployment spec says every service has one Git repository,
  pinned dependencies, env-var config, stateless processes, stdout logs, and
  one-off migrations run from the same release; classify those axes as
  `covered`.
- `implicit`: A small worker inherits the documented platform baseline for
  dependency isolation, logging, graceful shutdown, and release promotion;
  classify those axes as `implicit` if the baseline is present.
- `gap`: A SaaS feature spec references database credentials, background jobs,
  and scheduled maintenance scripts but does not define config separation,
  stateless processes, or admin-process execution; classify `config`,
  `processes`, and `admin_processes` as `gap`.

Use recommended kinds as guidance. Do not invent dimensions outside the twelve
listed above.
