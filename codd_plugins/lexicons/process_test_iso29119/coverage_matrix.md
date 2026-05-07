# ISO/IEC/IEEE 29119 Software Testing Coverage Matrix

Source: ISO/IEC/IEEE 29119 software testing series.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `test_concepts_definitions` | Shared vocabulary | General concepts, key concepts, and testing terms are explicit. | Test words are used without agreed meaning or scope. |
| `test_processes` | Controlled testing process | Organizational, management, dynamic, and risk-based test processes are defined. | Testing is only an activity list with no process control. |
| `test_documentation` | Audit artifacts | Test plans, designs, cases, procedures, results, and reports are documented or templated. | Test evidence cannot be reviewed or repeated. |
| `test_techniques` | Test design methods | Test design techniques and their use in design and implementation are specified. | Test cases have no stated derivation method. |
| `keyword_driven_testing` | Reusable keyword tests | Keyword specifications, frameworks, tools, data exchange, and hierarchy are defined where relevant. | Automated test behavior cannot be reused or exchanged. |
| `work_aided_software_testing` | Supported test work | Work products, supporting tools, reviews, automation, or lifecycle aids are explicit. | Test work depends on unsupported manual judgment or missing work products. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`. Findings are
emitted only for `gap`.

