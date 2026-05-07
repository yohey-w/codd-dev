# ISO/IEC 25010 Product Quality Coverage Matrix

Source: ISO/IEC 25010 software product quality model.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `functional_suitability` | Required functions | Completeness, correctness, and appropriateness are explicit. | Features are listed without task or correctness criteria. |
| `performance_efficiency` | Time, resources, capacity | Response time, throughput, resource use, and capacity are specified. | Performance is only described as fast or scalable. |
| `compatibility` | Coexistence and interoperability | Shared environments and exchanged information are defined. | Integrations are named without compatibility expectations. |
| `usability` | Human interaction quality | Recognizability, learnability, operability, error protection, and accessibility are defined. | The interface is described without user-operation quality criteria. |
| `reliability` | Dependable operation | Availability, fault tolerance, maturity, and recovery are specified. | Failure or recovery behavior is absent. |
| `security` | Information protection | Confidentiality, integrity, non-repudiation, accountability, and authenticity are explicit. | Security is asserted without protected properties. |
| `maintainability` | Change quality | Modularity, analysability, modifiability, reusability, and testability are defined. | Change impact or testability is not addressed. |
| `portability` | Environment movement | Adaptability, installability, and replaceability are specified. | Target environments are unclear or migration is unplanned. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`. Findings are
emitted only for `gap`.

