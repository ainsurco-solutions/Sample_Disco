# Support Model

[← Operations](README.md) | [Home](../README.md)

---

## Support tiers

| Tier | Scope | Team | Contact |
|---|---|---|---|
| **L1 – Service Desk** | User queries, password resets, known issues | | |
| **L2 – Application Support** | Functional issues, data queries, config | | |
| **L3 – Engineering** | Code defects, infrastructure, DB | | |
| **Vendor / Third Party** | External component issues | | |

---

## Incident management

| Attribute | Detail |
|---|---|
| Incident tool | <!-- e.g. ServiceNow, Jira Service Desk --> |
| P1 response SLA | <!-- e.g. 15 minutes --> |
| P2 response SLA | |
| P3 response SLA | |
| On-call rota | <!-- Coverage hours, escalation path --> |
| Major incident process | <!-- Bridge call, comms cadence, RCA requirement --> |
| Post-incident review | <!-- Required for P1? Template location? --> |

---

## Escalation path

```
L1 Service Desk
    │  (unresolved after SLA)
    ▼
L2 Application Support
    │  (code / infra issue confirmed)
    ▼
L3 Engineering
    │  (third-party component)
    ▼
Vendor Support
```

---

## Known recurring issues

| Issue | Frequency | Workaround | Root cause | Fix planned? |
|---|---|---|---|---|
| <!-- e.g. Batch job lock timeout --> | Weekly | Restart service X | Index contention | No |
| <!-- Add issues --> | | | | |

> Keep this table current — it is the first reference for L1/L2 triage.

---

[← Testing](testing.md) | [Next: DR & BCP →](dr-bcp.md)
