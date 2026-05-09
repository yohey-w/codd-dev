# Twelve-Factor App Coverage Matrix

Source: 12factor.net methodology for software-as-a-service applications.

| Axis | Covered when | Implicit when | Gap when |
| --- | --- | --- | --- |
| `codebase` | One revision-controlled codebase maps to many deploys. | A platform repository convention is cited and applies to the app. | Multiple repos, generated artifacts, or deploys are described without codebase ownership. |
| `dependencies` | Dependencies are explicitly declared and isolated from system packages. | A package manager or buildpack baseline is named and accepted. | Runtime libraries, CLIs, or OS packages are assumed without declaration. |
| `config` | Deploy-varying config and secrets are stored outside code, typically in env vars. | A named secrets or environment baseline is referenced and applies. | Credentials, hostnames, feature flags, or deploy settings appear in code or static files. |
| `backing_services` | Databases, queues, caches, storage, and external APIs are attached resources addressed through config. | The platform injects resources through a documented service-binding mechanism. | Services are hardwired, not swappable, or missing locator and credential handling. |
| `build_release_run` | Build artifacts, release config, and run execution are distinct and auditable. | The deployment platform's release lifecycle is cited and applies. | Build-time, release-time, and runtime behavior are mixed or unclear. |
| `processes` | Processes are stateless and share nothing, with persistent state in backing services. | The runtime is a stateless function or worker platform with documented persistence rules. | Local filesystem, memory sessions, or sticky processes are required without safeguards. |
| `port_binding` | The service exports HTTP or another protocol through a bound port. | The platform adapter provides a documented equivalent service binding. | The service exposure model depends on undeclared container, webserver, or host assumptions. |
| `concurrency` | Work is split into process types that can scale horizontally. | The hosting model provides equivalent horizontal scaling semantics. | Scale-out, worker separation, or background processing is unstated. |
| `disposability` | Startup is fast, shutdown is graceful, and crashes or restarts are tolerated. | The runtime baseline defines lifecycle hooks and the app has no long-running state. | Deploys, interrupts, or crashes can lose work or leave inconsistent state. |
| `dev_prod_parity` | Development, staging, and production stay similar in time, personnel, and tooling. | A shared environment baseline or preview environment policy is cited. | Local-only dependencies, stale staging, or production-only services are required. |
| `logs` | Logs are written as event streams to stdout or stderr and routing is external. | The platform captures all process output as the log stream. | The app manages log files, storage, rotation, or hidden sinks without operational contract. |
| `admin_processes` | Migrations, maintenance, and consoles run as one-off processes from the same release and config. | The platform's task runner is cited and uses the same release. | Admin scripts run from laptops, separate images, or undocumented credentials. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
