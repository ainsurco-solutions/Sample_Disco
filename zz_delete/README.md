# Reconcile

Was every in-scope source database imported, and then archived?

Compares the master source register against **two** target lists -- the IRP
Platform (what landed via Data Bridge) and Data Vault (what is actually
archived) -- and puts every row into exactly one outcome. Built for
[TASK-0055](../../docs/planning/tasks/0055-reconciliation-store-and-comparison.md),
the comparison half of
[TASK-0016](../../docs/planning/tasks/0016-target-reconciliation-list.md).

```powershell
pip install -r requirements.txt
python -m streamlit run app.py
```

Upload the files in the sidebar, press **Reconcile**. Sample files
(`sample_master.csv`, `sample_platform.csv`, `sample_vault.csv`) are here to try
it against.

## The inputs

**Master source register** — needs a database-name column. `exists_on_server`
and `is_in_scope` are used when present; any other column is carried through and
displayed, never interpreted.

**IRP Platform list** — what landed via Data Bridge. Observed fields:
`exposureSetName`, `serverType`, `serverName`, `serverId`, `createdBy`,
`createdAt`, `updatedBy`, `updatedAt`, `thumbprint`, `tagIds`, `metrics_*`.

**Data Vault list** *(optional)* — what is actually archived. Observed fields:
`exposureName`, `exposureId`, `uri`, `status`, `databaseName`, `ownerName`,
`exposureSetId`, `exposureSetName`, the same server/audit fields, and
`metrics_dbsize_actual`.

Both target lists are produced by hand today; by
[TASK-0054](../../docs/planning/tasks/0054-target-list-api-extraction.md)'s API
extraction once that lands.

### You pick the key column

The two target lists do not share a key, and **nobody has confirmed which field
holds the source database name**: Platform carries `exposureSetName` and no
exposure name at all, while Data Vault carries `exposureName`, `databaseName`
*and* `exposureSetName`.

So the tool guesses, visibly, and you confirm the column on upload. Nothing is
hardcoded. When TASK-0054 confirms the real field, the guess becomes a default —
it does not become an assumption buried in the loader.

Master header spellings are matched loosely (`database name`, `db_name`,
`name`…) so an upstream rename does not break the tool. That is aliasing
*column headers* only — never data values.

## The outcomes

Every master row lands in exactly one, and every unmatched target row becomes an
orphan:

| Outcome | Meaning |
| :--- | :--- |
| **Missing** | In scope, on the server, on neither target. Work not started. |
| **Not archived** | Imported to the platform but **not** in the vault. Work half done. |
| **Orphaned** | On a target, unknown to the register. |
| **Duplicate** | The same name twice on one side. Reported, never resolved. |
| Not on server | In scope, but `exists_on_server` is not Y — cannot migrate it yet. |
| Out of scope | `is_in_scope` is not Y — excluded, not a gap. |
| **Archived** | In the vault. **The only outcome that means done.** |
| Imported | On the platform, no vault list supplied — archiving unconfirmed. |

The first four need a human; the rest are expected. A run with only out-of-scope
and not-on-server rows reports **clean** — those are different answers from
"missing", and folding them together would inflate the one number anyone acts on.

### Only Archived means done

Per [GLOSSARY.md](../../docs/GLOSSARY.md), **Data Bridge is the route in and
Data Vault is the destination**, and archiving is a *separate explicit step that
is not automatic on import* ([open question
13](../../docs/api/open-questions-for-moodys.md)). A database on the platform but
not in the vault is **imported, not migrated** — reporting it complete would
report a state the client has not got, when the client requires archiving with
five-year retention.

Open question 13 item 8 records that standard users cannot even view the archive
in the UI to confirm a database archived successfully. That is the gap this
comparison closes.

**Load no vault list and rows read `Imported`, never `Archived`.** The tool
states what it checked rather than inferring archiving from a platform hit.
Supplying an *empty* vault list is a different claim — "nothing is archived" —
and produces `Not archived`.

**Orphaned** usually means Amlin migrated that database themselves via
[the interim manual process](../../docs/operations/interim-manual-upload-process.md)
without telling AInsurco file-by-file.
[TASK-0026](../../docs/planning/tasks/0026-off-hours-scheduling-and-orphan-detection.md)
needs exactly this to avoid re-migrating their work.

**Duplicate** exists because two target exposures sharing a name would otherwise
satisfy one master row and show as done, hiding the uniqueness problem
TASK-0016 asks about directly. A duplicate is never matched.

## Matching is exact

Case-folded, whitespace-stripped, and **nothing else**. No fuzzy matching, no
separator normalisation, no suggestions.

Estate names are inconsistent enough (`AMLINUK` vs `Amlin`, `LIABLITY` vs
`LIABILITY`) that a near-match is a guess, and a plausible wrong pairing is far
harder to spot than an obvious gap. Same reasoning as
[`parse_names.py`](../Excel_parser/), which matches companies by name and never
by position. `test_reconcile.py` pins this: if `AMLINUK` ever starts matching
`AMLIN_UK`, a test fails.

> ⚠️ **Before reading a `Missing` count as fact**, confirm whether Moody's renames
> exposures on import. If it does, exact matching reports every row `Missing` and
> the number is an artefact of the rename rather than a real gap. That answer is
> TASK-0054's to produce.

Unrecognised and blank Y/N values read as **No**, which excuses a row from the
`Missing` count rather than inventing a finding.

## Missing columns

Only three columns are actually required: a name column in the master, and a key
column in each target list. Everything else is optional and carried through.

| Situation | What happens |
| :--- | :--- |
| Master has no name column | **Error**, listing what it looked for and what it found |
| Target has no usable key column | **Error** — pick one explicitly |
| You pick a column that isn't in the file | **Error**, naming it |
| Target file is empty | **Error**: no header row |
| `exists_on_server` / `is_in_scope` absent | Every row treated as in scope and present — **warned** |
| Flag column present but mostly blank | Blank reads as No — **warned** |
| Flag column holds `TBC`, `pending`, … | Reads as No — **warned**, values quoted back |
| Any other column absent | Fine — display-only |

> ⚠️ **A missing flag column and a blank one fail in opposite directions.** An
> absent column leaves rows *in* scope, so they stay in the `Missing` count. A
> blank value reads as No and excuses the row entirely. The second is the
> dangerous one: a register whose blanks mean *unknown* rather than *no* empties
> the findings and the run reports **clean**, which looks exactly like success.

That is why the load reports what it saw. Warnings appear both on upload and
beside the counts, because they change what the counts mean. A column that is
blank on more than half its rows triggers one; a single blank row in ten does
not.

## What gets stored

A SQLite file (`reconcile.db`, gitignored) holding each loaded file as an
immutable snapshot and each comparison as a run against two or three of them.
Re-running creates a new run; it never edits an old one.

Each snapshot records **which column was used as its key**, because a run
compared on `exposureSetName` and one compared on `databaseName` are not
comparable and nothing else would say which was used.

That history answers the question a throwaway view cannot: **is this row newly
missing, or has it been missing all along?** The app's *Compare with an earlier
run* panel does that diff.

The store holds real estate database names — it stays on the machine that ran it.

## Files

| File | Purpose |
| :--- | :--- |
| `reconcile.py` | Loading, matching, outcomes, CSV export. No Streamlit import. |
| `store.py` | SQLite snapshots and runs. No Streamlit import. |
| `app.py` | The Streamlit UI — a rendering layer only. |
| `test_reconcile.py` | 59 tests over the core and the store. |
| `spec` | Why it works this way. Read before changing the matching rule. |

```powershell
python -m pytest test_reconcile.py -q
```

## The API seam

`TargetLoader` in `reconcile.py` is the boundary. Everything downstream takes a
list of `Row` and does not care where it came from; `load_target_from_csv` is
today's only implementation.

When TASK-0054 lands, a second implementation calls
`GET /platform/riskdata/v1/exposures`, returns the same `Row` objects, and
nothing else in this tool changes. That mirrors
[CONTRIBUTING.md](../../CONTRIBUTING.md)'s rule that only `adapters/` knows
Moody's exists.

## What this is not

- It does **not** call the Moody's API — that is TASK-0054.
- It does **not** compare sizes or record counts. The `metrics_*` fields are
  carried through and displayed, never compared: that is a harder question,
  currently blocked on open questions 11–12, and the two sides are not even
  comparable on size (Data Vault carries `metrics_dbsize_actual`; the platform
  does not).
- It does **not** write to the registry.
- It is **not** the Migration Hub's own reconciliation, which runs inside
  `controls close` against live API results
  ([runbook](../../docs/operations/runbook.md#reconciliation)). That one is
  automatic and per-run; this is manual and on-demand. Two different things
  called reconciliation — keep them apart.
