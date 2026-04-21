# Functional Capabilities

[← Functional](README.md) | [Home](../README.md)

---

## Calculation engine

| Attribute | Detail |
|---|---|
| Location | <!-- Assembly / service name, repo path --> |
| Trigger | <!-- e.g. API call, batch job, on-demand --> |
| Inputs | <!-- Risk data fields, factor tables, rating parameters --> |
| Outputs | <!-- Premium components, loadings, taxes --> |
| Rating tables stored in | <!-- Database tables, flat files, config --> |
| Override capability | <!-- Manual load/discount, referral queue --> |

### Rating factor sources

| Factor group | Source table / file | Refresh frequency |
|---|---|---|
| <!-- e.g. Vehicle ratings --> | <!-- e.g. dbo.VehicleRatingFactors --> | <!-- e.g. Annually --> |
| <!-- e.g. Postcode risk --> | | |
| <!-- e.g. Claims loading --> | | |

---

## New business

| Attribute | Detail |
|---|---|
| Entry points | <!-- Portal, API, batch upload, manual --> |
| Quote validity period | <!-- e.g. 30 days --> |
| Auto-bind rules | <!-- Value thresholds, risk criteria --> |
| Referral conditions | <!-- What triggers manual UW review? --> |

**Process flow:**

1. Risk capture
2. Rating engine call
3. Quote generation
4. <!-- Bind / refer decision -->
5. Policy issue + document generation

---

## Policy administration

| Function | Description | Automated? |
|---|---|---|
| Mid-Term Adjustment (MTA) | Changes to in-force policy | Partial |
| Renewal generation | Automatic renewal offer production | Yes – batch |
| Renewal invite | Communication to policyholder | <!-- Yes/No --> |
| Cancellation – customer request | Pro-rata refund calculation | <!-- Yes/No --> |
| Cancellation – non-payment | Grace period + notice rules | <!-- Yes/No --> |
| Lapse at renewal | No renewal payment received | <!-- Yes/No --> |
| Reinstatement | Reactivating a lapsed policy | <!-- Yes/No --> |

---

## Claims

**Lifecycle:** `FNOL → Triage → Assessment → Settlement / Repudiation → Closure`

| Stage | Key activities | System component |
|---|---|---|
| FNOL | Incident capture, fraud screening | <!-- Component name --> |
| Triage | Peril classification, handler assignment | |
| Assessment | Reserve setting, liability decision | |
| Settlement | Payment authorisation | |
| Repudiation | Denial letter, reason coding | |
| Recovery / Subrogation | Third-party recovery tracking | |
| Closure | Final reserve, file closure | |

---

## Servicing

Customer and policy servicing functions not covered under Policy Admin or Claims:

- Document requests (policy schedules, certificates)
- Address / contact detail changes
- Bank detail updates
- Duplicate document reissue
- Complaints handling
- <!-- Add others -->

---

## Investments

| Attribute | Detail |
|---|---|
| In scope? | <!-- Yes / No / Partial --> |
| Investment types managed | <!-- e.g. premium float, reserving investment --> |
| System component | <!-- Component name --> |
| External investment platform | <!-- Yes/No, provider name --> |

---

## Disbursements

| Type | Trigger | Frequency | Output method |
|---|---|---|---|
| Refund to policyholder | Cancellation | Ad-hoc | BACS |
| Claim settlement | Settlement approval | Ad-hoc | BACS / Cheque |
| Broker commission | Month-end | Monthly | BACS |
| <!-- Add types --> | | | |

---

## Payments & receivables

| Function | Description |
|---|---|
| Premium collection – direct debit | <!-- Provider, schedule, retry rules --> |
| Premium collection – card | <!-- Provider, PCI scope --> |
| Instalment management | <!-- Interest-bearing? External credit provider? --> |
| Returned payments | <!-- NSF handling, policy suspension rules --> |
| Reconciliation | <!-- Frequency, automated vs manual --> |
| Aged debt management | <!-- Escalation rules --> |

---

## Reinsurance

| Attribute | Detail |
|---|---|
| Reinsurance types in scope | <!-- e.g. Quota share, excess of loss, facultative --> |
| Bordereaux production | <!-- Frequency, format, destination --> |
| RI premium calculation | <!-- System-calculated or manual --> |
| RI recoveries tracked | <!-- Yes / No --> |

---

## Commissions

| Type | Basis | Calculation | Payment cycle |
|---|---|---|---|
| Broker commission | <!-- % of NWP --> | <!-- System / Manual --> | Monthly |
| Override commission | <!-- Volume-based --> | | |
| Introducer fee | | | |

---

[← Users & Access](users-and-access.md) | [Next: Transaction Volumes →](transaction-volumes.md)
