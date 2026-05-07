# HIPAA Compliance Coverage Matrix

Sources: HIPAA Security Rule, 45 CFR Part 164 Subpart C, and Breach Notification Rule, 45 CFR Part 164 Subpart D.

| Axis | Covered when | Implicit when | Gap when |
| --- | --- | --- | --- |
| `Administrative safeguards` | Security management, assigned responsibility, workforce controls, awareness/training, incident procedures, contingency planning, evaluation, and documentation are stated. | The material documents that no ePHI is created, received, maintained, or transmitted. | ePHI exists but administrative responsibility, policies, workforce controls, or contingency coverage is missing. |
| `Physical safeguards` | Facility, workstation, device, and media safeguards are stated for systems and equipment handling ePHI. | ePHI processing is fully delegated to a documented compliant environment with no local systems, devices, or media. | ePHI systems or devices are described without physical access, workstation, or media controls. |
| `Technical safeguards` | Access control, audit controls, integrity, authentication, and transmission-security controls are specified. | No electronic PHI is in scope. | ePHI is stored, processed, or transmitted without technical safeguard coverage. |
| `Risk analysis` | Potential risks and vulnerabilities to ePHI confidentiality, integrity, and availability are assessed and tied to risk management. | The system explicitly excludes ePHI and documents why HIPAA Security Rule risk analysis is out of scope. | ePHI or a covered workflow exists without accurate and thorough risk analysis. |
| `Access control` | Unique user identification, emergency access, automatic logoff, and encryption/decryption expectations are specified as applicable. | There is no system access to ePHI. | Users, services, or software can access ePHI but access-control mechanisms are absent. |
| `Audit controls` | Mechanisms record and examine ePHI system activity, with logs or access reports available for review. | The design has no ePHI information system activity. | ePHI activity occurs without audit logs, access reports, or review procedures. |
| `Integrity` | ePHI alteration/destruction protection and authenticity mechanisms are stated. | No ePHI is stored or processed in mutable electronic form. | ePHI can be modified or destroyed without integrity controls or verification. |
| `Transmission security` | Transmitted ePHI has unauthorized-access safeguards, integrity controls, and encryption where appropriate. | No ePHI is transmitted over electronic communications networks. | ePHI is exchanged over networks without transmission-security controls. |
| `Breach notification` | Breach identification, individual/media/Secretary notices, business-associate notice, timing, content, and documentation are specified. | No unsecured PHI exists and breach handling is owned by a documented upstream regulated entity. | PHI exists but breach notification workflow, timing, or ownership is missing. |
| `Business associate contracts or other arrangements` | Business associate contracts or equivalent arrangements bind applicable safeguards, subcontractors, and incident reporting. | No business associate creates, receives, maintains, or transmits ePHI on behalf of the covered entity. | Vendors or subcontractors handle ePHI without contract or incident-reporting obligations. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
