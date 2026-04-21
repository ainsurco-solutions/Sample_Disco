# Change & Release Management

[← Governance](README.md) | [Home](../README.md)

---

## Change process

| Stage | Description | Approver(s) |
|---|---|---|
| Change request raised | Developer / BA raises RFC | |
| Technical review | Architecture / lead review | |
| Business sign-off | Product owner / business sponsor | |
| CAB | <!-- Formal CAB or lightweight process? --> | |
| Deployment scheduling | Agreed maintenance window | |
| Post-deploy verification | Smoke tests, monitoring check | |
| Rollback decision point | <!-- Criteria and time window --> | |

---

## Release types

| Type | Description | Frequency | Approvals required |
|---|---|---|---|
| Standard release | Planned feature / fix release | <!-- e.g. Fortnightly --> | Full CAB |
| Hotfix | Critical production defect | Ad-hoc | Emergency CAB |
| Config change | Non-code change (e.g. rating table update) | Ad-hoc | Business approval |
| Infrastructure change | Server, network, DB changes | Ad-hoc | CAB + Infra lead |

---

## Deployment pipeline

| Step | Tool | Environment sequence |
|---|---|---|
| Source control | <!-- e.g. Git / TFS / Azure DevOps --> | |
| Build | <!-- e.g. MSBuild, Azure Pipelines --> | |
| Automated tests | | DEV → SIT |
| Staging deploy | | STAGING |
| UAT sign-off | | UAT |
| Production deploy | | PROD |

---

## Rollback procedure

| Attribute | Detail |
|---|---|
| Rollback mechanism | <!-- Re-deploy previous artefact, DB snapshot restore --> |
| Rollback decision owner | |
| Maximum rollback window | <!-- e.g. Within 2 hours of deploy --> |
| DB migration rollback | <!-- Scripts maintained? Forward-only migrations? --> |

---

## Change freeze periods

| Period | Dates | Reason |
|---|---|---|
| <!-- e.g. Year-end freeze --> | <!-- Dec 20 – Jan 5 --> | Financial year-end |
| <!-- e.g. Renewal peak --> | <!-- Jan 1–31 --> | High volume period |
| <!-- Add periods --> | | |

---

[← Regulation](regulation.md) | [Next: Glossary →](../glossary.md)
