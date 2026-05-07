# Schema.org SEO Coverage Matrix

Source: Schema.org full hierarchy.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `Organization` | Organization identity | Organization or a relevant subtype is declared for publisher, business, or institutional identity. | Site identity is important but no organization type is chosen. |
| `Person` | Person identity | Person or a relevant person subtype is declared for authors, experts, or profiles. | Human identity is used in content but not structured. |
| `Product` | Product entity | Product or a product subtype is declared for commerce or catalog content. | Product pages omit product entity type. |
| `Article` | Editorial content | Article or relevant article subtype is declared. | Articles or posts lack a Schema.org article type. |
| `BreadcrumbList` | Site path | BreadcrumbList and ListItem coverage is explicit. | Navigation hierarchy is shown but not structured. |
| `FAQPage` | Question and answer page | FAQPage, Question, and Answer coverage is explicit. | FAQ content exists without question/answer structure. |
| `Event` | Event entity | Event or event subtype is declared. | Event pages omit event entity type. |
| `VideoObject` | Video media | VideoObject or relevant media subtype is declared. | Video content lacks structured video entity coverage. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
