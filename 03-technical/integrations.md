# Integrations & Interfaces

[← Technical](README.md) | [Home](../README.md)

---

## Inbound interfaces

| Interface name | Source system | Protocol | Format | Frequency | Description |
|---|---|---|---|---|---|
| <!-- BrokerQuoteAPI --> | Broker Portal | REST / HTTPS | JSON | Real-time | Quote requests |
| <!-- ClaimsIntake --> | Claims portal | SOAP | XML | Real-time | FNOL submission |
| <!-- VehicleDataFeed --> | DVLA / MID | SFTP | CSV | Daily | Vehicle lookups |
| <!-- Add interfaces --> | | | | | |

---

## Outbound interfaces

### Reports

| Report name | Consumer | Frequency | Format | Delivery |
|---|---|---|---|---|
| <!-- Management MI --> | Finance / Board | Monthly | Excel / PDF | Email / SharePoint |
| <!-- Lloyd's bordereaux --> | Reinsurer | Monthly | CSV / XLSX | SFTP |
| <!-- Add reports --> | | | | |

### Extracts

| Extract name | Consumer | Frequency | Format | Delivery |
|---|---|---|---|---|
| <!-- Policy data extract --> | Data warehouse | Nightly | Flat file | SFTP / DB link |
| <!-- Claims FNOL feed --> | Fraud analytics | Real-time | JSON | API / queue |
| <!-- Add extracts --> | | | | |

### Letters & documents

| Document type | Trigger | Generation method | Delivery channel |
|---|---|---|---|
| Policy schedule | Policy issue / MTA | <!-- Template engine name --> | Post / Email / Portal |
| Renewal invitation | Renewal batch | | |
| Claims acknowledgement | FNOL | | |
| Cancellation notice | Cancellation | | |
| Arrears notice | Payment failure | | |
| <!-- Add documents --> | | | |

---

## Integration architecture

| Attribute | Detail |
|---|---|
| Integration middleware | <!-- e.g. MuleSoft, BizTalk, Azure Service Bus, point-to-point --> |
| Message queuing | <!-- e.g. MSMQ, RabbitMQ, Azure Service Bus --> |
| Error / retry handling | <!-- Dead-letter queues, retry count, alerting --> |
| API Gateway | <!-- Present? Product name? --> |
| Auth between systems | <!-- Certificates, API keys, OAuth --> |

---

## Interface map

```
External                    Platform                      Outbound
--------                    --------                      --------
Broker Portal  ──REST──►   API Gateway   ──────────►   Data Warehouse
DVLA/MID       ──SFTP──►   Core System   ──SFTP──────► Reinsurers
Claims Portal  ──SOAP──►   Claims Svc    ──Email/Post─► Policyholders
                            Batch Jobs    ──SFTP──────► Banks (DD)
```

> Replace with actual architecture diagram when available.

---

[← Transaction Volumes](../02-functional/transaction-volumes.md) | [Next: Data Model →](data-model.md)
