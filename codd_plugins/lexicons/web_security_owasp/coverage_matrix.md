# Web Security OWASP Coverage Matrix

This matrix maps the lexicon axes to OWASP Top 10:2021 risks and ASVS 4.0.3
verification areas. Use it as reviewer guidance for coverage-mode output.

| Axis | Source literal | Covered when requirements state | Gap signal |
| --- | --- | --- | --- |
| `broken_access_control` | A01:2021 Broken Access Control | Authorization rules, object access, privilege boundaries, and secure failure behavior are explicit. | Protected resources or role boundaries are absent or client-only. |
| `cryptographic_failures` | A02:2021 Cryptographic Failures | Sensitive data protection, key handling, storage, and transport expectations are explicit. | Sensitive data exists without encryption, key, or transport requirements. |
| `injection` | A03:2021 Injection | Interpreter-boundary defenses and safe query or command construction are explicit. | Untrusted input reaches an interpreter without a stated defense. |
| `insecure_design` | A04:2021 Insecure Design | Threat-driven security decisions, abuse cases, and control ownership are explicit. | Security relies on implementation detail without a design control. |
| `security_misconfiguration` | A05:2021 Security Misconfiguration | Hardened defaults, required headers, disabled features, and deployment settings are explicit. | Runtime or deployment security defaults are not specified. |
| `vulnerable_and_outdated_components` | A06:2021 Vulnerable and Outdated Components | Dependency inventory, update, and vulnerability response expectations are explicit. | Component lifecycle or vulnerability handling is absent. |
| `identification_and_authentication_failures` | A07:2021 Identification and Authentication Failures | Identity, credential, recovery, and account protection behavior is explicit. | Login, recovery, or identity proofing is underspecified. |
| `software_and_data_integrity_failures` | A08:2021 Software and Data Integrity Failures | Update, build, plugin, data integrity, and deserialization protections are explicit. | Trusted code or data can change without integrity checks. |
| `security_logging_and_monitoring_failures` | A09:2021 Security Logging and Monitoring Failures | Security event logging, alerting, audit trails, and incident review are explicit. | Security events are not logged, monitored, or retained. |
| `server_side_request_forgery_ssrf` | A10:2021 Server Side Request Forgery (SSRF) | Outbound request target validation and network/resource boundaries are explicit. | User-controlled URLs or metadata drive outbound requests without allow lists. |
| `authentication` | V2 Authentication | Authenticator, credential storage, recovery, and service authentication controls are explicit. | Authentication is named but verification controls are absent. |
| `session_management` | V3 Session Management | Session token creation, storage, binding, expiry, and re-authentication are explicit. | Token lifetime, storage, or invalidation behavior is absent. |
| `input_validation` | V5.1 Input Validation | Positive validation, typing, length, range, schema, or parameter handling is explicit. | Input shape or trust boundary is omitted. |
| `output_encoding_and_injection_prevention` | V5.3 Output Encoding and Injection Prevention | Context-aware encoding, escaping, parameterization, or interpreter-safe handling is explicit. | Rendering, query, command, or document output lacks a stated defense. |
