# Purpose & Scope

[← Overview](README.md) | [Home](../README.md)

---

## Why this document exists

This wiki captures the **AS-IS state** of the legacy general insurance platform. It serves three primary purposes:

| Purpose | Description |
|---|---|
| **Operations** | Reference for teams running, monitoring, and maintaining the platform day-to-day |
| **Support** | Triage guide for diagnosing and resolving incidents |
| **Future Target State** | Baseline from which migration, re-platform, or new-build decisions can be made |

---

## Documentation dimensions

| Dimension | Coverage |
|---|---|
| **Functional** | Business capabilities: what the platform does |
| **Non-Functional** | Performance, availability, security, scalability |
| **Integrations** | All inbound and outbound interfaces |
| **Data Model** | Entities, data types, retention, compliance |

---

## Technology summary

| Attribute | Detail |
|---|---|
| Primary language | C# (.NET) |
| Data store | SQL Server |
| Architecture style | <!-- e.g. Monolith / SOA / Hybrid --> |
| Key engines | Calculation engine <!-- + others --> |
| Source control | <!-- e.g. Git / TFS / Azure DevOps --> |
| CI/CD | <!-- e.g. Azure Pipelines / Jenkins --> |

---

## Audience

This documentation is written for **engineering and development teams**. It assumes familiarity with general insurance concepts and C#/.NET platforms.

For business-facing documentation, see <!-- link to business docs if applicable -->.

---

[← Overview](README.md) | [Next: Users & Access →](../02-functional/users-and-access.md)
