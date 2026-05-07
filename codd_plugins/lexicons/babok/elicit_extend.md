---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: babok
observation_dimensions: 13
---

# BABOK 13 Observation Dimensions

Apply the base elicitation prompt, then inspect the material through these
requirements elicitation dimensions. For every dimension, judge whether there is
an explicit statement ("明示的記述があるか") and whether likely omissions remain
("抜け漏れがないか"). Add findings only when the source material supports the
observation or clearly shows missing information.

1. `stakeholder`: Identify business owners, operators, customers, regulators,
   administrators, and affected external parties. Note unidentified or
   conflicting stakeholder roles.
2. `goal`: Identify business goals, expected outcomes, success measures, KGI,
   KPI, and decision criteria. Note goals that lack measurable targets.
3. `flow`: Identify current and desired workflows, handoffs, approvals,
   exceptions, and operational triggers. Note missing flow steps or unclear
   ownership.
4. `issue`: Identify pain points, root causes, business background, current
   constraints, and unresolved problems. Note vague or unsupported issue
   statements.
5. `data`: Identify entities, attributes, lifecycle states, retention needs,
   ownership, import/export paths, and privacy-sensitive data. Note missing
   data definitions.
6. `functional`: Identify functions, user actions, system behaviors,
   integrations, notifications, search/filtering, reporting, and automation.
   Note underspecified behavior.
7. `non-functional`: Identify performance, availability, security, auditability,
   accessibility, localization, maintainability, scalability, and operability
   expectations. Note missing quality targets.
8. `rule`: Identify business rules, calculations, eligibility conditions,
   permissions, validation rules, routing rules, and exception rules. Note
   unclear rule sources or precedence.
9. `constraint`: Identify legal, compliance, budget, schedule, technology,
   migration, data residency, operational, and organizational constraints. Note
   implicit constraints that need confirmation.
10. `acceptance`: Identify acceptance criteria, review gates, testable outcomes,
    evidence requirements, and done conditions. Note ambiguous acceptance
    language.
11. `risk`: Identify delivery, adoption, operational, security, data, vendor,
    integration, and compliance risks. Note absent mitigations or owners.
12. `assumption`: Identify assumptions about users, process, data quality,
    dependencies, availability, permissions, and external systems. Note
    assumptions that require validation.
13. `term`: Identify domain terminology, abbreviations, synonyms, overloaded
    words, naming conventions, and glossary gaps. Note terms that need shared
    definitions.

Use the recommended kinds as guidance, not as a fixed vocabulary. The core
engine accepts dynamic `kind` values. Prefer the closest recommended kind when
it communicates the finding clearly; otherwise choose a concise, evidence-based
kind.

For each added finding, include:

- The dimension that triggered it in `details.dimension`.
- The exact source evidence or the missing evidence signal in `details.evidence`.
- A reviewer-facing question when human confirmation is needed.
- A rationale that explains the decision impact.

## Coverage-mode classification

When this BABOK lexicon is loaded, switch the L0 prompt into coverage-check
mode. Walk the 13 dimensions above and classify each one as:

- `covered`: requirements explicitly state expectations for this dimension.
- `implicit`: requirements imply enough context to proceed without a finding.
- `gap`: requirements omit this dimension and a clarification is required.

Emit findings ONLY for dimensions classified as `gap`. Populate the JSON object
output's `lexicon_coverage_report` with the full mapping using each dimension's
short identifier as the key (e.g. `stakeholder`, `goal`, `flow`, ..., `term`).
Set `all_covered` to `true` only when every dimension is `covered` or `implicit`
and `findings` is empty. Otherwise keep `all_covered` set to `false`.

When a finding is emitted because a dimension is a `gap`:

- Set `details.dimension` to that dimension's identifier.
- Choose a concise dynamic `kind` (recommended kinds in `recommended_kinds.yaml`
  may be used as guidance but are never required).
- Severity defaults to `medium`; raise to `high`/`critical` for safety,
  compliance, data isolation, or release-blocking ambiguity.

Do not invent extra dimensions outside this list. Project-specific findings
that fall outside the 13 dimensions should set `severity: info` and
`details.note: "outside_lexicon_scope"`.
