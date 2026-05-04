# Requirement Coverage Audit Report

**Project Type**: Tool/SaaS
**Summary**: AUTO_ACCEPT=0, ASK=1, AUTO_REJECT=3, PENDING=0
**Generated**: 2026-05-04T04:42:39.445811Z

## AUTO_ACCEPT

Items that are safe to adopt automatically because they are baseline requirements.


## ASK

Items that require human scope, priority, or applicability decisions.

- **disaster_recovery** (Disaster recovery RPO/RTO)
  - Question: Are there RPO/RTO requirements for backup and disaster recovery?
  - Provenance: inferred
  - Confidence: 0.70

## AUTO_REJECT

Items that are recorded as out of scope to prevent accidental implementation.

- **soc2_audit** (SOC 2 audit)
  - Reason: A local developer tool does not need certification by default.
  - Provenance: inferred
  - Confidence: 0.95
- **hipaa** (HIPAA compliance)
  - Reason: Out of scope unless protected health information is handled.
  - Provenance: inferred
  - Confidence: 0.98
- **pci_dss** (PCI-DSS)
  - Reason: Out of scope unless direct cardholder-data handling is introduced.
  - Provenance: inferred
  - Confidence: 0.90
