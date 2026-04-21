# DR & Business Continuity

[← Operations](README.md) | [Home](../README.md)

---

## Recovery objectives

| Attribute | Target | Last tested |
|---|---|---|
| RTO (Recovery Time Objective) | <!-- e.g. 4 hours --> | |
| RPO (Recovery Point Objective) | <!-- e.g. 1 hour --> | |

---

## DR configuration

| Attribute | Detail |
|---|---|
| DR site | <!-- Location, warm / cold / active-active --> |
| Failover process | <!-- Automated / manual, runbook location --> |
| Last DR test | <!-- Date, outcome --> |
| DR test frequency | |
| BCP document location | |

---

## Critical functions — recovery priority order

| Priority | Function | RTO | Dependencies |
|---|---|---|---|
| 1 | <!-- e.g. Policy bind / new business --> | | |
| 2 | <!-- e.g. Claims FNOL --> | | |
| 3 | <!-- e.g. Direct debit collection --> | | |
| 4 | <!-- e.g. Renewals --> | | |
| 5 | <!-- e.g. Reporting / MI --> | | |

---

## Communication plan

| Trigger | Who is notified | Method | Owner |
|---|---|---|---|
| P1 incident declared | <!-- CTO, Head of Ops --> | <!-- Phone / Slack --> | |
| DR invoked | <!-- Board, FCA if required --> | | |
| Customer impact confirmed | <!-- Marketing, Contact Centre --> | | |
| Incident resolved | <!-- All stakeholders --> | | |

---

## Runbooks

| Scenario | Runbook location |
|---|---|
| Database failover | <!-- Link --> |
| Application server failover | <!-- Link --> |
| Complete site loss | <!-- Link --> |
| Data restore from backup | <!-- Link --> |

---

[← Support](support.md) | [Next: Regulation →](../05-governance/regulation.md)
