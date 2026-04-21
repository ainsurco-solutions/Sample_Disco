# Testing & Environments

[← Operations](README.md) | [Home](../README.md)

---

## Environment inventory

| Environment | Purpose | Data | Access |
|---|---|---|---|
| **PROD** | Live | Live customer data | Restricted |
| **Staging** | Final validation before release | Masked prod copy | Dev + QA |
| **UAT** | User acceptance testing | Synthetic / masked | Business + Dev |
| **SIT** | System integration testing | Synthetic | Dev + QA |
| **DEV** | Active development | Synthetic | Dev |
| **PERF** | Load / stress testing | Scaled synthetic | Dev + QA |

---

## Test types & ownership

| Test type | Tooling | Owner | Frequency |
|---|---|---|---|
| Unit tests | <!-- e.g. NUnit, xUnit --> | Dev | Every build |
| Integration tests | | Dev / QA | Every build |
| SIT | | QA | Per sprint / release |
| UAT | | Business | Per release |
| Regression | | QA | Per release |
| Performance / Load | <!-- e.g. JMeter, k6 --> | Dev / QA | Major releases |
| Security / DAST | | Security / Dev | <!-- Frequency --> |
| Smoke tests (post-deploy) | | Dev / Ops | Every deployment |

---

## Test data management

| Attribute | Detail |
|---|---|
| Test data source | <!-- Synthetic / masked prod / manually curated --> |
| PII masking approach | |
| Test data refresh frequency | |
| Test data tooling | |

---

## Defect management

| Attribute | Detail |
|---|---|
| Defect tracking tool | <!-- e.g. Jira, Azure DevOps --> |
| Severity classification | <!-- P1–P4 or similar --> |
| SLA for fix by severity | |
| Regression trigger threshold | <!-- e.g. any P1/P2 defect --> |

---

[← Hosting](../03-technical/hosting.md) | [Next: Support →](support.md)
