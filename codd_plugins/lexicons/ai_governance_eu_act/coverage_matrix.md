# EU AI Act Governance Coverage Matrix

Source: Regulation (EU) 2024/1689 on artificial intelligence.

| Axis | Covered when | Implicit when | Gap when |
| --- | --- | --- | --- |
| `Classification rules for high-risk AI systems` | The AI system's risk class, Annex III relevance, and significant-risk reasoning are stated. | The material explicitly states no AI system is being placed on the market, put into service, or deployed. | AI features exist but risk class or high-risk reasoning is absent. |
| `Prohibited AI practices` | The design excludes or mitigates Article 5 practices such as manipulation, vulnerability exploitation, social scoring, or banned biometric uses. | The material describes non-AI automation with no AI inference or targeting. | AI behavior could intersect Article 5 but no prohibition check is stated. |
| `Requirements for high-risk AI systems` | Risk management, data governance, technical documentation, logging, transparency to deployers, accuracy, robustness, and cybersecurity are specified. | The system is explicitly not high-risk and the classification rationale is documented. | A high-risk or likely high-risk system lacks Section 2 requirement coverage. |
| `Human oversight` | Natural-person oversight, intervention, interpretation, and misuse handling are specified. | The system has no autonomous recommendation, prediction, or decision support. | AI outputs affect people but oversight is not described. |
| `Transparency obligations for providers and deployers of certain AI systems` | AI interaction notices, synthetic-content labeling, emotion-recognition disclosure, and deployer communications are specified where applicable. | No user-facing AI, synthetic content, or listed transparency scenario exists. | Users or affected persons may encounter AI or synthetic content without disclosure rules. |
| `General-purpose AI models` | GPAI scope, documentation, downstream information, copyright policy, and systemic-risk obligations are specified. | The system uses no GPAI model and does not distribute a model. | A foundation, general-purpose, or reusable model is used or provided without GPAI duties. |
| `Conformity assessment` | Required assessment route, internal control or notified-body path, CE/declaration artifacts, and significant-change triggers are specified. | The system is not subject to high-risk conformity assessment and that conclusion is documented. | High-risk placement, service, or major change lacks conformity assessment. |
| `Post-market monitoring by providers and post-market monitoring plan` | Monitoring plan, lifecycle metrics, incident capture, serious-incident reporting, and corrective action are specified. | The system is not placed on market or operated as an AI system after release. | AI system operation continues after release without monitoring or incident handling. |
| `Fundamental rights impact assessment for high-risk AI systems` | FRIA scope, affected groups, risks, mitigations, and deployer responsibilities are specified. | No high-risk deployer context applies and the reason is documented. | High-risk deployment by an in-scope deployer lacks fundamental-rights impact review. |
| `Governance at Union and national level` | AI literacy, accountable roles, competent-authority contact, market-surveillance response, and governance escalation are specified. | A non-AI internal tool has no external governance interface. | AI obligations exist but owner, literacy, or authority interface is missing. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
