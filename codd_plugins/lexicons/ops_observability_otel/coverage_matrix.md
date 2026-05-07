# OpenTelemetry Observability Coverage Matrix

Source: OpenTelemetry Specification plus OpenTelemetry Semantic Conventions and
Collector Architecture.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `signals_traces` | Trace telemetry | Traces, spans, context, links, events, and trace identity expectations are explicit. | Distributed requests are observable only as prose or logs without span behavior. |
| `signals_metrics` | Metric telemetry | Meters, instruments, measurements, views, and metric reader/export behavior are explicit. | Health, latency, throughput, or saturation is named without metric instruments or aggregation. |
| `signals_logs` | Log telemetry | Log data model, log records, logger provider, and export expectations are explicit. | Operational events exist but log record shape or routing is unspecified. |
| `context_propagation` | Correlation across boundaries | Context, propagators, Baggage, and carrier behavior are explicit. | Cross-service telemetry cannot be correlated or carrier behavior is absent. |
| `resource_attributes` | Telemetry entity identity | Resource attributes such as `service.name` and environment configuration are explicit. | Telemetry cannot be attributed to services, hosts, workloads, or processes. |
| `instrumentation` | Application and library integration | API, SDK, instrumentation scope, and library instrumentation ownership are explicit. | Code is expected to emit telemetry but integration ownership or API boundary is absent. |
| `collector` | Receive-process-export path | Collector pipelines, receivers, processors, and exporters are explicit. | Telemetry is generated but ingestion, transformation, or export path is undefined. |
| `semantic_conventions` | Standard telemetry vocabulary | Semantic conventions, attribute naming, requirement levels, and convention groups are explicit. | Attribute names or event/span/metric vocabulary are local and ungoverned. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
