---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: process_test_iso29119
observation_dimensions: 6
---

# ISO/IEC/IEEE 29119 Software Testing Coverage Lexicon

Apply the base elicitation prompt, then inspect requirements and design notes
through the 6 ISO/IEC/IEEE 29119 testing-process axes declared in
`lexicon.yaml`.

1. `test_concepts_definitions`
2. `test_processes`
3. `test_documentation`
4. `test_techniques`
5. `keyword_driven_testing`
6. `work_aided_software_testing`

For every axis, classify coverage as:

- `covered`: the material explicitly states the testing-series concept,
  process, document, technique, keyword-driven approach, or aided work product
  and its evidence expectation.
- `implicit`: the material references a shared ISO/IEC/IEEE 29119 test policy,
  test-management baseline, documentation pack, or tooling baseline that is
  present in the same source set and clearly covers the axis.
- `gap`: the material omits the testing-process detail needed to judge test
  vocabulary, process control, documentation, test design, keyword reuse, or
  aided work support.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing testing behavior requires human
confirmation. Severity follows `severity_rules.yaml`.

## Coverage-check examples

### covered

Requirement: "System testing follows the risk-based organizational and test
management processes, produces test design specifications and test completion
reports, and derives cases using boundary value analysis."

Classification: `covered` for `test_processes`, `test_documentation`, and
`test_techniques`.

### implicit

Requirement: "The acceptance suite follows the attached `test-standard-v2`
baseline, including terminology, keyword-driven test specifications, tool
support, and work product review records."

Classification: `implicit` for `test_concepts_definitions`,
`keyword_driven_testing`, and `work_aided_software_testing` when the referenced
baseline is available in the same source set.

### gap

Requirement: "QA will test the feature before release."

Classification: `gap` for all axes because the material does not define test
concepts, processes, documentation, techniques, keyword-driven specifications,
or aided test work products.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.

