# Terraform IaC Coverage Matrix

Source: Terraform Language v1.7.x plus HCP Terraform Sentinel policy enforcement.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `terraform` | Settings block | required_version, required_providers, backend, or provider_meta expectations are explicit. | Configuration compatibility or backend setup is needed but no terraform block contract is named. |
| `provider` | Provider dependency and configuration | provider, alias, source, version, or configuration_aliases are explicit. | Resource or data behavior depends on a provider but source/version/configuration is absent. |
| `resource` | Managed infrastructure objects | resource blocks and meta-arguments such as count, for_each, depends_on, or lifecycle are explicit. | Infrastructure should be created or changed without a managed resource contract. |
| `data` | External information queries | data resources, dependencies, preconditions, or postconditions are explicit. | Configuration reads external information but query timing or assumptions are unstated. |
| `variable` | Module inputs and outputs | variable, output, type, default, sensitive, and validation behavior are explicit. | A module boundary is expected without input, output, type, or sensitive value contract. |
| `state` | Infrastructure-state mapping | state, terraform.tfstate, moved, import, or terraform state operations are explicit. | Existing resources, refactors, or state custody are required but unmodeled. |
| `module` | Reusable configuration | module, source, version, providers, and outputs are explicit. | Reuse is expected without source, version, provider passing, or output contract. |
| `workspace` | State partitioning | workspace, terraform.workspace, default workspace, or workspace state behavior is explicit. | Multiple environments share configuration without state selection guidance. |
| `backend` | Persistent state storage | backend, local or remote mode, partial configuration, and terraform init behavior are explicit. | State storage or initialization is required but unspecified. |
| `Sentinel` | Policy enforcement | Sentinel, policy set, enforcement level, and tfplan/tfconfig/tfstate/tfrun imports are explicit. | Policy-as-code is expected without policy data, grouping, or enforcement behavior. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
