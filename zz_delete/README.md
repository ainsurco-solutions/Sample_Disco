# Reconcile

Was every in-scope source database imported, and then archived?

Compares an on-prem SQL inventory against two target lists — the IRP Platform
(what landed via Data Bridge) and Data Vault (what is actually archived) — and
puts every row into exactly one outcome.

```powershell
pip install -r requirements.txt
python -m streamlit run app.py
```

Then load the lists in the sidebar and press **Run reconciliation**.

---

## This is a generated copy — do not edit it here

These files are produced from the project's master copy with all comments and
docstrings stripped, and are **overwritten in place** every time they are
re-published.

A fix made in this folder is lost on the next push. It is also made blind: the
comments that were removed are the ones explaining *why* the code is the way it
is — why the vault matches on `archiveName`, why a size field that looks like
bytes is refused, why paging stops on a short page. Without them it is easy to
"fix" something that was deliberate.

If something here is wrong, raise it with the AInsurco team rather than editing
it. The change belongs in master and will come back on the next publish.

---

## Configuration

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored and must
never be committed — it holds a live API key.

```ini
RECONCILE_API_HOST=https://api-euw1.rms.com
MOODYS_API_KEY=<your key>
RECONCILE_SQL_DIRECT_HOST=<server>
RECONCILE_SQL_RI_HOST=<server>
```

SQL access uses **integrated security** — the logged-in Windows user. There is
no username or password anywhere in this tool.

`reconcile.db` is created beside the app on first run and holds real database
names from your estate. It is gitignored and belongs on the machine that ran
it.

---

## The four inputs

| | | |
| :--- | :--- | :--- |
| **1. Source** | On-prem inventory | Required. Upload a CSV or press **Query server**. |
| **2. Scope** | Databases expected to move | Optional. May name databases absent from the inventory. |
| **3. Data Bridge** | What landed on the platform | Required. Upload a CSV. |
| **4. Data Vault** | What is confirmed archived | Optional. Upload a CSV or press **Call API**. |

Without step 4 the tool cannot say anything about archiving, and says so:
rows come back **Archiving unchecked**, not **Archived**. Archiving is a
separate explicit step on the platform, not automatic on import, so inferring
it from a Data Bridge hit would assert something untrue.

---

## Matching

Exact, case-folded. No fuzzy matching and no separator normalisation — a name
either matches or it does not.

That is deliberate. Fuzzy matching here would pair `AAANORTHEAST_RDM_...` with
`AAANORTHEAST_PORT_RDM_...` and report a database as migrated when it was not,
which is worse than reporting a miss: a miss gets investigated, a false match
does not.

It does mean the inventory and the target lists must use the same names,
character for character.

### Duplicate names in Data Vault

The vault accepts more than one archive under the same name — archiving a
database twice is supported, not an error. Two archives with one name cannot be
matched to a single database, so both are reported **Duplicate** and neither
matches.

The **Vault duplicates** tab lists these with the fields that differ within
each group, so you can tell "the same database archived twice" from "two
different things sharing a name".

---

## Troubleshooting

**`CERTIFICATE_VERIFY_FAILED` on an API call.** A TLS-inspecting corporate
proxy. Install `python-certifi-win32` (it is in `requirements.txt`). Do **not**
use `verify=False` — that disables certificate checking altogether and sends
the API key over a connection nobody is authenticating.

**`Query server` fails but everything else works.** `pyodbc` is optional and
needs an ODBC driver that pip cannot install. Upload a CSV instead — that path
always works.

**A 4xx from the archive API.** The error now includes the server's own
message. `python probe_archive_api.py` sends the same request several times,
removing one parameter each, to show which one is refused. It is read-only.

**Everything reports Missing.** Almost always the wrong match field, not a real
finding. Check that the Data Vault list is keyed on the field holding your
database names.
