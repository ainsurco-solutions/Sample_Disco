#!/usr/bin/env python3
"""
app.py
======

Streamlit UI for the reconciliation tool. Run with:

    python -m streamlit run app.py

A rendering layer only. Every decision about what a row means lives in
reconcile.py, and everything about persistence lives in store.py -- both
Streamlit-free and tested on their own (test_reconcile.py). If you find
yourself writing an `if` about outcomes in this file, it probably belongs in
reconcile.py instead.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from reconcile import (
    FINDINGS,
    LoadError,
    Outcome,
    Reconciliation,
    guess_key_column,
    load_master,
    load_target_from_csv,
    reconcile,
    target_headers,
    to_csv,
)
from store import DEFAULT_DB_PATH, Store

st.set_page_config(page_title="Reconcile", page_icon="\U0001F50E", layout="wide")

#: Colour per outcome. Findings are red/amber, everything expected is neutral --
#: "out of scope" reading as a warning would be exactly the confusion the
#: separate outcomes exist to prevent.
OUTCOME_COLOUR: dict[Outcome, str] = {
    Outcome.MISSING: "#b3261e",
    Outcome.NOT_ARCHIVED: "#c25e00",
    Outcome.ORPHANED: "#b26a00",
    Outcome.DUPLICATE: "#7b4ea8",
    Outcome.NOT_ON_SERVER: "#5f6368",
    Outcome.OUT_OF_SCOPE: "#5f6368",
    Outcome.ARCHIVED: "#1e7b34",
    Outcome.IMPORTED: "#3a6ea5",
}

OUTCOME_HELP: dict[Outcome, str] = {
    Outcome.MISSING: (
        "In scope, on the server, on neither target. Work not started."
    ),
    Outcome.NOT_ARCHIVED: (
        "Imported to the platform but not archived in Data Vault. "
        "Work half done — archiving is a separate step, not automatic."
    ),
    Outcome.ORPHANED: (
        "On a target with no row in the register. "
        "Often a database Amlin migrated themselves."
    ),
    Outcome.DUPLICATE: (
        "The same name twice on one side. Reported, never resolved automatically."
    ),
    Outcome.NOT_ON_SERVER: (
        "In scope, but not on the source server yet. Cannot migrate it."
    ),
    Outcome.OUT_OF_SCOPE: "Excluded from the migration. Not a gap.",
    Outcome.ARCHIVED: "In the vault. The only outcome that means done.",
    Outcome.IMPORTED: (
        "On the platform. No vault list was supplied, so archiving is "
        "unconfirmed — this is not the same as done."
    ),
}


@st.cache_resource
def get_store() -> Store:
    """One Store for the app's lifetime. Streamlit reruns the script on every
    interaction, so this must not reopen the database each time."""
    return Store(DEFAULT_DB_PATH)


def _init_state() -> None:
    st.session_state.setdefault("master_rows", None)
    st.session_state.setdefault("target_rows", None)
    st.session_state.setdefault("vault_rows", None)
    st.session_state.setdefault("master_warnings", [])
    st.session_state.setdefault("master_label", "")
    st.session_state.setdefault("target_label", "")
    st.session_state.setdefault("vault_label", "")
    st.session_state.setdefault("result", None)
    st.session_state.setdefault("run_id", None)


_init_state()
store = get_store()


def results_frame(reconciliation: Reconciliation) -> pd.DataFrame:
    """Result rows as a table, original columns included."""
    records = []
    for row in reconciliation.rows:
        record = {
            "Outcome": row.outcome.value,
            "Name": row.name,
            "Side": row.source,
            "Note": row.note,
        }
        for key, value in row.data.items():
            if not key.startswith("_"):
                record.setdefault(key, value)
        records.append(record)
    return pd.DataFrame(records)


# --------------------------------------------------------------------------
# Sidebar: load the three sides
# --------------------------------------------------------------------------

def _load_target_side(
    label: str,
    caption: str,
    state_rows: str,
    state_label: str,
    upload_key: str,
    picker_key: str,
) -> None:
    """Upload one target list and pick its key column.

    The picker is not a convenience. Which field holds the source database
    name is unconfirmed -- the platform list carries exposureSetName and no
    exposure name, the vault carries exposureName, databaseName and
    exposureSetName -- so the tool guesses visibly and a person confirms,
    rather than deciding in code. TASK-0054 owns the real answer.
    """
    st.subheader(label)
    st.caption(caption)
    uploaded = st.file_uploader(label, type=["csv"], key=upload_key, label_visibility="collapsed")
    if uploaded is None:
        return

    text = uploaded.getvalue().decode("utf-8-sig")
    headers = target_headers(text)
    if not headers:
        st.error("That file has no header row.")
        st.session_state[state_rows] = None
        return

    guess = guess_key_column(headers)
    index = headers.index(guess) if guess in headers else 0
    chosen = st.selectbox(
        "Match on which column?",
        headers,
        index=index,
        key=picker_key,
        help="Guessed from the headers. Confirm it -- nothing else checks this.",
    )
    try:
        rows = load_target_from_csv(text, chosen)
        st.session_state[state_rows] = rows
        st.session_state[state_label] = uploaded.name
        st.success(f"{len(rows)} rows, matching on `{chosen}`")
    except LoadError as exc:
        st.session_state[state_rows] = None
        st.error(str(exc))


with st.sidebar:
    st.title("\U0001F50E Reconcile")
    st.caption("Was every in-scope source database imported, and then archived?")

    st.subheader("1. Master source register")
    master_file = st.file_uploader(
        "Master CSV",
        type=["csv"],
        key="master_upload",
        help=(
            "Needs a database-name column. exists_on_server and is_in_scope "
            "are used if present."
        ),
    )
    if master_file is not None:
        try:
            loaded = load_master(master_file.getvalue().decode("utf-8-sig"))
            st.session_state.master_rows = list(loaded)
            st.session_state.master_label = master_file.name
            st.session_state.master_warnings = loaded.warnings()
            st.success(f"{len(loaded)} databases")
            for flag in loaded.flags:
                if flag.found and flag.should_warn:
                    st.warning(
                        f"`{flag.column}` is blank on {flag.blank} of "
                        f"{flag.total} rows — those rows will be excused from "
                        "the findings."
                    )
        except LoadError as exc:
            st.session_state.master_rows = None
            st.session_state.master_warnings = []
            st.error(str(exc))

    _load_target_side(
        "2. IRP Platform list",
        "What landed via Data Bridge. The route in, not the destination.",
        "target_rows",
        "target_label",
        "target_upload",
        "target_key",
    )

    _load_target_side(
        "3. Data Vault list (optional)",
        (
            "What is actually archived. Leave this out and the tool reports "
            "*Imported* rather than *Archived* — it will not claim archiving "
            "it did not check."
        ),
        "vault_rows",
        "vault_label",
        "vault_upload",
        "vault_key",
    )

    st.divider()
    ready = (
        st.session_state.master_rows is not None
        and st.session_state.target_rows is not None
    )
    if st.button("Reconcile", type="primary", disabled=not ready, use_container_width=True):
        master_rows = st.session_state.master_rows
        target_rows = st.session_state.target_rows
        vault_rows = st.session_state.vault_rows
        result = reconcile(master_rows, target_rows, vault_rows)
        master_id = store.save_snapshot(
            "master",
            st.session_state.master_label,
            st.session_state.master_label,
            master_rows,
        )
        target_id = store.save_snapshot(
            "platform",
            st.session_state.target_label,
            st.session_state.target_label,
            target_rows,
        )
        vault_id = None
        if vault_rows is not None:
            vault_id = store.save_snapshot(
                "vault",
                st.session_state.vault_label,
                st.session_state.vault_label,
                vault_rows,
            )
        st.session_state.result = result
        st.session_state.run_id = store.save_run(
            master_id, target_id, result, vault_snapshot_id=vault_id
        )

    st.divider()
    st.caption(f"Store: `{DEFAULT_DB_PATH.name}`")
    st.caption(f"{len(store.runs())} run(s) saved")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

st.title("Source-to-target reconciliation")

result: Reconciliation | None = st.session_state.result

if result is None:
    st.info(
        "Upload a master source register and an IRP Platform list in the sidebar, "
        "then press **Reconcile**. Add a Data Vault list to check archiving too."
    )
    past_runs = store.runs()
    if past_runs:
        st.subheader("Earlier runs")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Run": r.id,
                        "Ran at (UTC)": r.ran_at,
                        "Master": r.master_label,
                        "Target": r.target_label,
                    }
                    for r in past_runs
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
        chosen = st.selectbox("Reopen a run", [r.id for r in past_runs])
        if st.button("Open"):
            st.session_state.result = store.load_run(int(chosen))
            st.session_state.run_id = int(chosen)
            st.rerun()

    with st.expander("What the outcomes mean"):
        for outcome, text in OUTCOME_HELP.items():
            st.markdown(f"**{outcome.value}** — {text}")
    st.stop()


counts = result.counts()

# Headline: the two findings first, because they are the reason to run this.
st.subheader("Summary")
cols = st.columns(len(Outcome))
for col, outcome in zip(cols, Outcome, strict=True):
    with col:
        st.markdown(
            f"<div style='color:{OUTCOME_COLOUR[outcome]};font-size:2rem;"
            f"font-weight:700;line-height:1'>{counts[outcome]}</div>"
            f"<div style='color:{OUTCOME_COLOUR[outcome]};font-weight:600'>"
            f"{outcome.value}</div>",
            unsafe_allow_html=True,
        )
        st.caption(OUTCOME_HELP[outcome])

scope_note = (
    f"{result.master_count} master rows against {result.target_count} platform rows"
)
if result.vault_checked:
    scope_note += f" and {result.vault_count} vault rows"
scope_note += f". Run {st.session_state.run_id}."
st.caption(scope_note)

if not result.vault_checked:
    st.warning(
        "**No Data Vault list supplied**, so nothing here confirms archiving. "
        "Rows show as *Imported*, which is not the same as done — archiving is "
        "a separate, explicit step that is not automatic on import."
    )

# How the master file was read. These change what the counts below mean, so
# they belong beside the numbers, not only at upload time.
for warning in st.session_state.master_warnings:
    st.warning(f"**Master register:** {warning}")

if result.is_clean:
    st.success(
        "Nothing needs attention. Out-of-scope and not-on-server rows are expected, "
        "not gaps."
    )
else:
    finding_count = len(result.findings)
    st.warning(f"{finding_count} row(s) need a human.")

st.divider()

# Filter and browse.
st.subheader("Rows")
frame = results_frame(result)

left, right = st.columns([2, 1])
with left:
    default = [o.value for o in Outcome if counts[o] and o in FINDINGS] or [
        o.value for o in Outcome if counts[o]
    ]
    chosen_outcomes = st.multiselect(
        "Outcome",
        [o.value for o in Outcome],
        default=default,
        help="Findings are pre-selected.",
    )
with right:
    search = st.text_input("Search name", "")

view = frame[frame["Outcome"].isin(chosen_outcomes)] if chosen_outcomes else frame
if search:
    view = view[view["Name"].str.contains(search, case=False, na=False)]

st.dataframe(view, hide_index=True, use_container_width=True)
st.caption(f"Showing {len(view)} of {len(frame)} rows.")

st.download_button(
    "Download full reconciliation (CSV)",
    data=to_csv(result),
    file_name="reconciliation.csv",
    mime="text/csv",
)

# Comparing two runs is the reason the runs are stored at all.
past_runs = store.runs()
if len(past_runs) > 1:
    st.divider()
    st.subheader("Compare with an earlier run")
    st.caption("Is a Missing row new, or has it been missing all along?")
    ids = [r.id for r in past_runs]
    earlier = st.selectbox("Earlier run", ids, index=min(1, len(ids) - 1))
    later = st.selectbox("Later run", ids, index=0)
    if st.button("Compare"):
        diff = store.compare_runs(int(earlier), int(later))
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("**Changed outcome**")
            st.write(diff["changed"] or "—")
        with c2:
            st.markdown("**Newly appeared**")
            st.write(diff["appeared"] or "—")
        with c3:
            st.markdown("**No longer present**")
            st.write(diff["disappeared"] or "—")
