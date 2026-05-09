# ML Model Cards Coverage Matrix

Source: Mitchell et al., "Model Cards for Model Reporting" and the Google Model
Card Toolkit.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `model_details` | Model provenance | Model name, owner, architecture or type, version, and release context are explicit. | The model is referenced without traceable identity or ownership. |
| `intended_use` | Use boundary | Primary intended uses, users, and out-of-scope uses are stated. | The model can be applied without a documented use boundary. |
| `evaluation_factors` | Relevant groups and conditions | Evaluation factors, data slices, populations, domains, or conditions are declared. | Performance is reported without context about who or what was evaluated. |
| `performance_metrics` | Measured behavior | Metrics, thresholds, and task-specific measurements are documented. | Reviewers cannot judge behavior for the intended use. |
| `training_data` | Data provenance and preprocessing | Training data source, composition, and preprocessing are described. | Users cannot assess distribution fit or data provenance. |
| `ethical_considerations` | Bias, harm, and mitigation analysis | Bias, fairness, misuse, risk, and mitigation notes are documented. | The model has no recorded ethical or fairness analysis. |
| `caveats_recommendations` | Limitations and guidance | Known limitations, caveats, and recommendations guide deployment. | Users receive no caveats for unsafe or weak contexts. |
| `model_versioning` | Lifecycle control | Registry, version history, rollback, or update ownership is defined. | The deployed model cannot be traced or reverted confidently. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`. Findings are
emitted only for `gap`.
