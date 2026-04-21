# Transaction Volumes & Schedules

[← Functional](README.md) | [Home](../README.md)

---

## Volume benchmarks

| Metric | Value | Notes / source |
|---|---|---|
| Transactions per day (average) | <!-- TBC --> | |
| Transactions per day (peak) | <!-- TBC --> | |
| Transactions per hour (peak) | <!-- TBC --> | |
| Transactions per month | <!-- TBC --> | |
| Policies in force (PIF) | <!-- TBC --> | |
| Active claims | <!-- TBC --> | |

---

## Peak patterns

| Pattern | Detail |
|---|---|
| Peak days of week | <!-- e.g. Monday, end-of-month --> |
| Peak months / seasons | <!-- e.g. January renewals, storm season claims --> |
| End-of-day batch window | <!-- e.g. 22:00–04:00 --> |

---

## Scheduled batch jobs

| Job name | Description | Schedule | Avg duration | Owner |
|---|---|---|---|---|
| <!-- RenewalGeneration --> | Generates renewal invitations | <!-- Sunday 22:00 --> | <!-- 3 hrs --> | |
| <!-- DirectDebitCollection --> | Submits DD file to bank | <!-- Daily 18:00 --> | | |
| <!-- BordereauxExtract --> | RI reporting extract | <!-- 1st of month --> | | |
| <!-- DataArchive --> | Moves aged records to archive | <!-- Weekly Sunday --> | | |

> **Action:** Confirm full job list from SQL Agent / task scheduler / CI/CD pipeline.

---

## Ad-hoc transactions

| Type | Triggering event | Approx frequency |
|---|---|---|
| Manual policy correction | Data fix request | <!-- e.g. ~10/week --> |
| Emergency payment | Major loss event | Rare |
| <!-- Add types --> | | |

---

[← Functional Capabilities](functional-capabilities.md) | [Next: Integrations →](../03-technical/integrations.md)
