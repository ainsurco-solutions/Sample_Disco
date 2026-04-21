# Users & Access

[← Functional](README.md) | [Home](../README.md)

---

## User categories

| Category | Description | Example roles |
|---|---|---|
| **Internal** | Staff employed by the organisation | Underwriters, claims handlers, finance, IT |
| **External** | Third parties accessing the platform | Brokers, partners, reinsurers |
| **Back-office** | Operational and administrative users | Policy admin, billing, compliance |
| **Front-of-office** | Customer-facing or sales-facing users | Contact centre agents, broker portal users |

---

## Distribution channels

| Channel | Active? | Description |
|---|---|---|
| D2C (Direct to Consumer) | <!-- Yes/No --> | Customers self-serving via web or app |
| B2B (Business to Business) | <!-- Yes/No --> | Corporate clients purchasing direct |
| B2B2C | <!-- Yes/No --> | Partners selling on to end consumers |
| Agency | <!-- Yes/No --> | Appointed representatives |
| Partnership | <!-- Yes/No --> | White-label or affinity arrangements |

---

## Functional teams

| Team | Primary functions |
|---|---|
| Underwriting | <!-- e.g. Quote, referral decisions, MTA approvals --> |
| Policy Administration | <!-- e.g. Policy issue, endorsements, renewals --> |
| Claims | <!-- e.g. FNOL, assessment, settlement --> |
| Finance | <!-- e.g. Reconciliation, bordereaux, disbursements --> |
| Compliance | <!-- e.g. Audit, regulatory reporting --> |
| IT / Operations | <!-- e.g. Batch monitoring, deployments, support --> |

---

## Role-based access control (RBAC)

> **Action:** Extract full role list from application config / Active Directory / identity provider.

| Role name | Type | Permissions summary | Channel(s) |
|---|---|---|---|
| <!-- e.g. UW_SENIOR --> | Internal | <!-- Bind up to £500k, referral override --> | Back-office |
| <!-- e.g. CLAIMS_HANDLER --> | Internal | <!-- Reserve + settle up to £10k --> | Back-office |
| <!-- e.g. BROKER_USER --> | External | <!-- Quote, bind, MTA on own book --> | B2B Portal |

---

## Authentication & identity

| Attribute | Detail |
|---|---|
| Authentication method | <!-- e.g. LDAP, Azure AD, SSO, Forms auth, API key --> |
| MFA | <!-- Enabled / Not enabled / Partial --> |
| Session management | <!-- Timeout duration, token type --> |
| External user auth | <!-- e.g. separate IdP, broker portal auth mechanism --> |
| Service-to-service auth | <!-- e.g. shared secrets, certificates, OAuth client credentials --> |

---

[← Purpose & Scope](../01-overview/purpose-and-scope.md) | [Next: Functional Capabilities →](functional-capabilities.md)
