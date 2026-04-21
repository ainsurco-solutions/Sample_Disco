# Hosting & Infrastructure

[← Technical](README.md) | [Home](../README.md)

---

## Infrastructure summary

| Attribute | Detail |
|---|---|
| Hosting model | <!-- On-premise / Private cloud / Public cloud / Hybrid --> |
| Primary data centre | |
| DR / secondary site | |
| Server OS | <!-- e.g. Windows Server 2019 --> |
| Database platform | <!-- e.g. SQL Server 2019 Standard --> |
| Web / app server | <!-- e.g. IIS 10, Windows Service --> |
| Load balancing | |
| Storage | <!-- SAN, NAS, blob --> |
| Backup tooling & schedule | |
| Certificate management | <!-- Expiry monitoring? --> |
| Domain / DNS | |

---

## Environment inventory

| Environment | Purpose | URL / host | Database | Access |
|---|---|---|---|---|
| **PROD** | Live traffic | | | Restricted |
| **Staging** | Final validation | | | Dev + QA |
| **UAT** | Acceptance testing | | | Business + Dev |
| **SIT** | Integration testing | | | Dev + QA |
| **DEV** | Active development | | | Dev |
| **PERF** | Load / stress testing | | | Dev + QA |

---

## Monitoring & alerting

| Concern | Tool | Location / URL |
|---|---|---|
| Application logs | | |
| Infrastructure metrics | | |
| Alerting / on-call | | |
| Error tracking | | |

---

[← Non-Functional](non-functional.md) | [Next: Testing →](../04-operations/testing.md)
