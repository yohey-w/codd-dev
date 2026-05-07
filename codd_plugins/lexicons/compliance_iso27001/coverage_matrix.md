# ISO/IEC 27001 Compliance Coverage Matrix

Source: ISO/IEC 27001:2022 information security management systems requirements
and Annex A information security control vocabulary.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `context_organization` | ISMS context and scope | Context, interested parties, and ISMS scope are explicit. | Security scope or organizational boundary is unclear. |
| `leadership` | Accountability | Policy, roles, responsibilities, and leadership commitment are explicit. | Security ownership is not assigned. |
| `planning` | Risk and objective planning | Risks, opportunities, objectives, and change plans are defined. | Security objectives or risk planning are absent. |
| `support` | Enablers | Resources, competence, awareness, communication, and documents are controlled. | Required capability or documentation is missing. |
| `operation` | ISMS execution | Operational control, risk assessment, and treatment execution are defined. | Risk-management work is planned but not operated. |
| `performance_evaluation` | Assurance | Monitoring, measurement, internal audit, and management review are defined. | ISMS performance cannot be assessed. |
| `improvement` | Corrective loop | Nonconformity, corrective action, and continual improvement are defined. | Failures do not feed an improvement loop. |
| `risk_treatment_plan` | Treatment evidence | Treatment options, controls, owners, and residual risk are traceable. | Risk decisions lack treatment evidence. |
| `SOA` | Control applicability | Selected and excluded controls are justified. | Applicability of controls cannot be audited. |
| `access_control` | Authorized access | Identity, privilege, and information access controls are explicit. | Access rights are not governed. |
| `cryptography` | Cryptographic protection | Cryptographic controls and key management are defined. | Data protection depends on unspecified encryption. |
| `physical_security` | Facilities and equipment | Secure areas, facilities, and equipment controls are explicit. | Physical protection is assumed but not defined. |
| `supplier_relationships` | External parties | Supplier security requirements, monitoring, and agreements are explicit. | Supplier security duties are not governed. |
| `incident_management` | Event and incident handling | Reporting, response, evidence, and lessons learned are defined. | Security events have no response process. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`. Findings are
emitted only for `gap`.

