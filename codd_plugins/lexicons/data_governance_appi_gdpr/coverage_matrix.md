# GDPR and APPI Data Governance Coverage Matrix

Sources: GDPR (EU) 2016/679, PPC APPI tentative English translation, and PPC offshore-transfer guidelines.

| Axis | Covered when | Implicit when | Gap when |
| --- | --- | --- | --- |
| `Lawfulness of processing` | Each personal-data flow states a lawful basis or APPI handling justification. | The material explicitly states no personal data is processed. | Personal data is collected, used, or disclosed without a lawful basis or handling rule. |
| `Consent` | Consent collection, withdrawal, evidence, and APPI third-party or foreign-country consent needs are specified. | The design avoids consent-based processing and documents another basis. | Consent is relied on or likely needed but capture, proof, or withdrawal is absent. |
| `Purpose limitation` | Utilization purposes and compatible further processing rules are stated. | A single non-personal operational purpose is explicit and no personal data exists. | Personal data is collected or reused without specified purposes. |
| `Storage limitation` | Retention periods, erasure, and retained-personal-data treatment are specified. | The system only processes transient data and states no retention. | Personal data persists without retention or erasure expectations. |
| `Data subject rights` | Access, rectification/correction, erasure/deletion, restriction or cease-use, objection, and disclosure workflows are specified. | No retained or identifiable personal data exists. | Users or principals can be identified but rights handling is unstated. |
| `Responsibility of the controller` | Accountable owner, technical and organisational measures, APPI security control, and demonstrability are specified. | Processing is delegated to a documented upstream controller with no local means or purposes. | No accountable role, measures, or security-control ownership is stated. |
| `Processor` | Processor scope, documented instructions, sub-processor handling, assistance, deletion/return, and audit support are specified. | No service provider handles data on another party's behalf. | Vendors or services process personal data without processor obligations. |
| `Records of processing activities` | Processing records, transfer records, categories, purposes, recipients, and security measures are specified. | The processing falls outside record duties and the reason is documented. | Audit records or third-party provision records are not described. |
| `Data protection impact assessment` | High-risk processing triggers, DPIA content, risk assessment, safeguards, and review are specified. | The material explicitly excludes high-risk processing and explains why. | Profiling, special data, large-scale monitoring, or high-risk processing appears without DPIA coverage. |
| `Notification of a personal data breach` | Breach detection, escalation, 72-hour GDPR notification, APPI leakage reporting, affected-person notice, and documentation are specified. | No personal data is processed and breach handling is out of scope. | Personal data exists but breach notification and reporting are missing. |
| `Transfers of personal data to third countries or international organisations` | Third-country, international-organisation, or foreign-country provision safeguards, consent, and onward-transfer rules are specified. | Data is explicitly stored and accessed only in one jurisdiction. | Cloud, vendor, offshore, or international transfer paths are unclear. |
| `Data protection officer` | DPO designation, tasks, independence, contact path, or APPI inquiry contact is specified. | The organization documents why DPO designation is not required and names an alternate inquiry contact. | Compliance oversight or requester escalation ownership is missing. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
