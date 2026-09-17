# Generated copy -- do not edit.
# Source: dev-tools/Reconcile/app.py in the AML-MigHub repo.
# Comments and docstrings are stripped; the reasoning is in master.
# Re-run dev-tools/Reconcile/push_to_demo.py to update.

from __future__ import annotations

import html
import io
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import streamlit as st
from reconcile import (
    FINDINGS,
    LoadError,
    Outcome,
    QueryError,
    Reconciliation,
    funnel,
    funnel_to_csv,
    guess_key_column,
    load_inventory_from_sql,
    load_source_list,
    load_target_from_csv,
    load_vault_from_api,
    reconcile,
    size_of,
    target_headers,
    to_csv,
)
from settings import INVENTORY_QUERY, load_settings, load_sql_settings
from store import DEFAULT_DB_PATH, Store

API = load_settings()
SQL = load_sql_settings()

st.set_page_config(page_title="Reconcile", page_icon="\U0001F50E", layout="wide")

st.markdown(
    """
    <style>
      .rc-row { display:flex; flex-wrap:wrap; gap:.6rem; margin:.15rem 0 .1rem; }
      .rc-card {
        flex:1 1 140px; min-width:140px; padding:.6rem .8rem;
        border:1px solid color-mix(in srgb, var(--rc) 35%, transparent);
        border-left:4px solid var(--rc);
        border-radius:6px;
        background:color-mix(in srgb, var(--rc) 7%, transparent);
      }
      .rc-card-empty { opacity:.45; }
      .rc-count {
        color:var(--rc); font-size:1.9rem; font-weight:700; line-height:1.1;
      }
      .rc-name {
        color:var(--rc); font-weight:600; font-size:.85rem;
        white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
      }
      .rc-label {
        font-size:.75rem; font-weight:600; letter-spacing:.06em;
        text-transform:uppercase; opacity:.6; margin-bottom:.15rem;
      }
      .rc-label-spaced { margin-top:.9rem; }
      .rc-hint {
        font-weight:400; text-transform:none; letter-spacing:0; opacity:.75;
      }

      .fb-count {
        color:var(--rc); font-size:1.8rem; font-weight:700; line-height:1.1;
        text-align:center;
      }
      .fb-total { color:#b26a00; }
      .fb-total-ok { color:#1e7b34; }
      .fb-total-label {
        text-align:center; font-size:.72rem; font-weight:700;
        text-transform:uppercase; letter-spacing:.04em; margin-bottom:.15rem;
      }

      .hl-row {
        display:flex; flex-wrap:wrap; gap:.9rem; margin:.2rem 0 .9rem;
      }

      .hl-row-primary { margin-bottom:.55rem; }
      .hl-card {

        flex-grow:1; flex-shrink:1; min-width:170px; padding:.85rem 1rem;
        border:1px solid rgba(128,128,128,.28); border-radius:8px;
        background:rgba(128,128,128,.06);
      }
      .hl-count { font-size:2rem; font-weight:700; line-height:1.05; }

      .hl-row-primary .hl-card {
        padding:1.1rem 1.25rem; background:rgba(128,128,128,.1);
        border-color:rgba(128,128,128,.38);
      }
      .hl-row-primary .hl-count { font-size:2.9rem; }
      .hl-row-primary .hl-name { font-size:1rem; letter-spacing:.01em; }

      .hl-good { color:#1e7b34; }
      .hl-warn { color:#b26a00; }
      .hl-bad  { color:#b3261e; }
      .hl-name {
        font-weight:600; font-size:.92rem; margin-top:.1rem; line-height:1.25;
      }
      .hl-note { font-size:.78rem; opacity:.65; margin-top:.3rem; }

      .sp { padding:0; margin:0 0 .4rem; }

      .sp-head { display:flex; align-items:center; gap:.4rem; }
      .sp-num {
        display:inline-flex; align-items:center; justify-content:center;
        width:1.3rem; height:1.3rem; border-radius:50%;
        background:rgba(128,128,128,.28); font-size:.72rem; font-weight:700;
      }
      .sp-title { font-weight:700; font-size:.92rem; }
      .sp-badge {
        margin-left:auto; font-size:.62rem; font-weight:700;
        letter-spacing:.05em; text-transform:uppercase;
        padding:.08rem .35rem; border-radius:3px;
      }
      .sp-req { background:rgba(179,38,30,.13); color:#b3261e; }
      .sp-opt { background:rgba(128,128,128,.18); color:#5f6368; }
      .sp-label { font-size:.82rem; font-weight:600; margin-top:.2rem; }
      .sp-purpose { font-size:.75rem; opacity:.62; line-height:1.3; }
      .sp-state {
        font-size:.72rem; font-weight:700; margin-top:.3rem;
        text-transform:uppercase; letter-spacing:.04em;
      }
      .sp-detail {
        display:block; font-weight:400; text-transform:none;
        letter-spacing:0; opacity:.7; font-size:.72rem;
      }

      .sp-ready .sp-state { color:#1e7b34; }
      .sp-error .sp-state { color:#b3261e; }
      .sp-todo .sp-state { color:#b26a00; }
      .sp-skip .sp-state { opacity:.55; }

      section[data-testid="stSidebar"] [class*="st-key-step_"] {
        border:1px solid rgba(128,128,128,.25);
        border-left:3px solid #9aa0a6;
        border-radius:6px;
        padding:.55rem .65rem .3rem;
        margin-bottom:.4rem;
        background:rgba(128,128,128,.05);
      }
      section[data-testid="stSidebar"] [class*="st-key-step_"]:has(.sp-ready) {
        border-left-color:#1e7b34;
      }
      section[data-testid="stSidebar"] [class*="st-key-step_"]:has(.sp-error) {
        border-left-color:#b3261e; background:rgba(179,38,30,.06);
      }
      section[data-testid="stSidebar"] [class*="st-key-step_"]:has(.sp-todo) {
        border-left-color:#b26a00;
      }
      .sp-gate {
        font-size:.75rem; font-weight:600; text-align:center;
        padding:.3rem; border-radius:4px; margin-bottom:.4rem;
        background:rgba(178,106,0,.12); color:#b26a00;
      }
      .sp-gate-ok { background:rgba(30,123,52,.12); color:#1e7b34; }

      [data-testid="stFileUploader"] { width:100%; margin:0; }
      [data-testid="stFileUploaderDropzone"] {
        padding:0; border:none; background:transparent; min-height:0;
        width:100%;
      }
      [data-testid="stFileUploaderDropzoneInstructions"] { display:none; }

      [data-testid="stFileUploaderDropzone"] button {
        width:100%; margin:0; justify-content:center;
      }

      [data-testid="stFileUploaderFile"] small { display:none; }
      [data-testid="stFileUploaderFile"] { padding:.1rem 0; }

      section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
        gap:.35rem;
      }
      section[data-testid="stSidebar"] .stButton { width:100%; }
      section[data-testid="stSidebar"] .stButton button {
        padding:.25rem .5rem; min-height:0; width:100%;
      }

      section[data-testid="stSidebar"] [data-testid="stHorizontalBlock"] {
        gap:.4rem;
      }

      section[data-testid="stSidebar"] .stSelectbox > label,
      section[data-testid="stSidebar"] .stFileUploader > label {
        display:none;
      }
      section[data-testid="stSidebar"] hr { margin:.5rem 0; }

      .fn { margin:.6rem 0 1rem; }
      .fn-row {
        display:grid;
        grid-template-columns:7.5rem minmax(120px,1fr) 3.5rem minmax(0,1.6fr);
        align-items:center; gap:.75rem; padding:.3rem 0;
      }
      .fn-stage { font-weight:600; font-size:.9rem; }
      .fn-track {
        position:relative; background:rgba(128,128,128,.13);
        border-radius:4px; height:2rem; display:flex; align-items:center;
      }

      .fn-bar {
        position:absolute; inset:0 auto 0 0; border-radius:4px;
        background:rgba(128,128,128,.42);
      }
      .fn-bar-done { background:#2f6f4f; }

      .fn-value {
        position:relative; margin-left:auto; padding-right:.6rem;
        font-weight:700; font-size:.95rem;
      }
      .fn-drop { font-size:.85rem; font-weight:600; color:#b3261e; }

      .fn-share {
        font-weight:400; font-size:.78rem; opacity:.6; margin-left:.4rem;
      }
      .fn-kept {
        display:block; font-weight:400; font-size:.7rem; opacity:.6;
        color:inherit;
      }
      .fn-note { font-size:.82rem; opacity:.7; }
      @media (max-width: 900px) {
        .fn-row { grid-template-columns:6rem 1fr 3rem; }
        .fn-note { grid-column:1 / -1; padding-left:6.75rem; }
      }
    </style>
    """,
    unsafe_allow_html=True,
)

RED = "#b3261e"
AMBER = "#b26a00"
GREEN = "#1e7b34"
GREY = "#5f6368"

OUTCOME_COLOUR: dict[Outcome, str] = {
    Outcome.MISSING: RED,
    Outcome.DUPLICATE: RED,
    Outcome.NOT_ARCHIVED: AMBER,
    Outcome.NOT_IN_INVENTORY: AMBER,
    Outcome.ORPHANED: AMBER,
    Outcome.ARCHIVED: GREEN,
    Outcome.OUT_OF_SCOPE: GREY,
    Outcome.IMPORTED: GREY,
}

OUTCOME_HELP: dict[Outcome, str] = {
    Outcome.MISSING: (
        "In scope and on prem, but in neither Data Bridge nor Data Vault. "
        "Work not started."
    ),
    Outcome.NOT_ARCHIVED: (
        "In Data Bridge but not archived in Data Vault. In transit — "
        "archiving is a separate step, not automatic on import."
    ),
    Outcome.NOT_IN_INVENTORY: (
        "On the Scope list but absent from Source (On Prem). Either it was "
        "decommissioned or renamed, or the Source list is incomplete — "
        "not a migration failure."
    ),
    Outcome.ORPHANED: (
        "In Data Bridge or Data Vault with no row in Source or Scope. "
        "Often a database Amlin migrated themselves."
    ),
    Outcome.DUPLICATE: (
        "The same name twice on one side. Reported, never resolved automatically."
    ),
    Outcome.OUT_OF_SCOPE: (
        "On prem, but not on the Scope list. Not a gap."
    ),
    Outcome.ARCHIVED: (
        "In Data Vault. The only outcome that means done."
    ),
    Outcome.IMPORTED: (
        "In Data Bridge, but no Data Vault list was supplied — so whether it "
        "is archived is unknown, not confirmed and not denied."
    ),
}

@st.cache_resource
def get_store() -> Store:
    return Store(DEFAULT_DB_PATH)

def _init_state() -> None:
    st.session_state.setdefault("master_rows", None)
    st.session_state.setdefault("target_rows", None)
    st.session_state.setdefault("vault_rows", None)
    st.session_state.setdefault("scope_rows", None)
    st.session_state.setdefault("raw_text", {})
    st.session_state.setdefault("step_errors", {})
    st.session_state.setdefault("outcome_filter", None)
    st.session_state.setdefault("master_label", "")
    st.session_state.setdefault("target_label", "")
    st.session_state.setdefault("vault_label", "")
    st.session_state.setdefault("scope_label", "")
    st.session_state.setdefault("result", None)
    st.session_state.setdefault("run_id", None)

_init_state()
store = get_store()

def render_headline_cards(
    cards: list[tuple[str, int, str, str]], *, primary: bool = False
) -> None:
    basis = 100 / max(len(cards), 1)
    row_class = "hl-row hl-row-primary" if primary else "hl-row"
    html_cards = []
    for title, count, note, tone in cards:
        tone_class = f" hl-{tone}" if tone else ""
        html_cards.append(
            f"<div class='hl-card' style='flex-basis:calc({basis:.4f}% - .9rem)'>"
            f"<div class='hl-count{tone_class}'>{count}</div>"
            f"<div class='hl-name'>{html.escape(title)}</div>"
            f"<div class='hl-note'>{html.escape(note)}</div>"
            "</div>"
        )
    st.markdown(
        f"<div class='{row_class}'>{''.join(html_cards)}</div>",
        unsafe_allow_html=True,
    )

def render_finding_buttons(
    outcomes: list[Outcome],
    counts: dict[Outcome, int],
    finding_total: int,
    is_clean: bool,
) -> None:
    selected = st.session_state.outcome_filter
    columns = st.columns(len(outcomes) + 1)

    with columns[0]:
        tone = "fb-total-ok" if is_clean else "fb-total"
        st.markdown(
            f"<div class='fb-count {tone}'>{finding_total}</div>",
            unsafe_allow_html=True,
        )
        if st.button(
            "Show all",
            key="filter_btn_clear",
            use_container_width=True,
            type="primary" if selected is None else "secondary",
        ):
            st.session_state.outcome_filter = None
            st.rerun()

    for col, outcome in zip(columns[1:], outcomes, strict=True):
        count = counts[outcome]
        with col:
            st.markdown(
                f"<div class='fb-count' style='--rc:{OUTCOME_COLOUR[outcome]}'>"
                f"{count}</div>",
                unsafe_allow_html=True,
            )
            if st.button(
                outcome.value,
                key=f"filter_btn_{outcome.name}",
                disabled=not count,
                use_container_width=True,
                type="primary" if selected == outcome.value else "secondary",
                help=OUTCOME_HELP[outcome],
            ):
                st.session_state.outcome_filter = (
                    None if selected == outcome.value else outcome.value
                )
                st.rerun()

def render_outcome_cards(outcomes: list[Outcome], counts: dict[Outcome, int]) -> None:
    cards = []
    for outcome in outcomes:
        count = counts[outcome]
        colour = OUTCOME_COLOUR[outcome]
        dim = "" if count else " rc-card-empty"
        cards.append(
            f"<div class='rc-card{dim}' style='--rc: {colour}' "
            f"title='{html.escape(OUTCOME_HELP[outcome])}'>"
            f"<div class='rc-count'>{count}</div>"
            f"<div class='rc-name'>{html.escape(outcome.value)}</div>"
            f"</div>"
        )
    st.markdown(
        f"<div class='rc-row'>{''.join(cards)}</div>", unsafe_allow_html=True
    )

def render_file_tab(title: str, filename: str, text: str, key: str) -> None:
    rows = pd.read_csv(io.StringIO(text), dtype=str).fillna("")

    left, mid, right = st.columns(3)
    left.metric("Rows", len(rows))
    mid.metric("Columns", len(rows.columns))
    matched_on = ""
    if key != "master":
        stored = st.session_state.get(key) or []
        if stored:
            matched_on = stored[0].data.get("_key_column", "")
    right.metric("Matching on", matched_on or "—")

    st.caption(f"`{filename}`")

    search = st.text_input(
        "Filter rows containing", "", key=f"filter_{key}", placeholder="any text"
    )
    view = rows
    if search:
        mask = rows.apply(
            lambda r: r.astype(str).str.contains(search, case=False, na=False).any(),
            axis=1,
        )
        view = rows[mask]

    st.dataframe(view, hide_index=True, use_container_width=True, height=420)
    st.caption(f"Showing {len(view)} of {len(rows)} rows, every column as supplied.")

    left_dl, right_dl = st.columns(2)
    with left_dl:
        st.download_button(
            f"Download these {len(view)} rows (CSV)",
            data=view.to_csv(index=False),
            file_name=f"{Path(filename).stem}-filtered.csv",
            mime="text/csv",
            key=f"dl_filtered_{key}",
            use_container_width=True,
            disabled=len(view) == len(rows),
            help="What the table is showing, with the filter applied.",
        )
    with right_dl:
        st.download_button(
            "Download the file as supplied",
            data=text,
            file_name=filename,
            mime="text/csv",
            key=f"dl_raw_{key}",
            use_container_width=True,
            help="The original upload, byte for byte.",
        )

    with st.expander("Raw text, exactly as uploaded"):
        st.code(text, language="text")

def results_frame(reconciliation: Reconciliation) -> pd.DataFrame:
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

@dataclass(frozen=True)
class Step:

    number: str
    title: str
    label: str
    purpose: str
    required: bool
    kind: str
    state_rows: str
    state_label: str

STEPS: tuple[Step, ...] = (
    Step(
        "1", "Source", "On-prem inventory",
        "Every database that exists today.",
        True, "inventory", "master_rows", "master_label",
    ),
    Step(
        "2", "Scope", "Migration scope",
        "The databases expected to move.",
        False, "scope", "scope_rows", "scope_label",
    ),
    Step(
        "3", "Data Bridge", "IRP platform list",
        "Databases imported into the platform.",
        True, "target", "target_rows", "target_label",
    ),
    Step(
        "4", "Data Vault", "Archive list",
        "Databases confirmed as archived.",
        False, "target", "vault_rows", "vault_label",
    ),
)

def _step_state(step: Step) -> tuple[str, str, str]:
    error = st.session_state.step_errors.get(step.state_rows)
    if error:
        return "Error", error, "sp-error"
    rows = st.session_state.get(step.state_rows)
    if rows is None:
        if step.required:
            return "Not uploaded", "required", "sp-todo"
        return "Not uploaded", "optional — will be skipped", "sp-skip"
    filename = st.session_state.get(step.state_label) or "file"
    return "Ready", f"{filename} · {len(rows)} rows", "sp-ready"

def render_step(step: Step) -> None:
    state, detail, css = _step_state(step)
    badge = "Required" if step.required else "Optional"
    badge_css = "sp-req" if step.required else "sp-opt"

    card = st.container(border=True, key=f"step_{step.state_rows}")
    with card:
        st.markdown(
            f"<div class='sp {css}'>"
            f"<div class='sp-head'>"
            f"<span class='sp-num'>{step.number}</span>"
            f"<span class='sp-title'>{html.escape(step.title)}</span>"
            f"<span class='sp-badge {badge_css}'>{badge}</span>"
            f"</div>"
            f"<div class='sp-label'>{html.escape(step.label)}</div>"
            f"<div class='sp-purpose'>{html.escape(step.purpose)}</div>"
            f"<div class='sp-state'>{html.escape(state)}"
            f"<span class='sp-detail'>{html.escape(detail)}</span></div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    can_fetch = step.kind in ("target", "inventory")
    api_col = card if can_fetch else None

    with card:
        uploaded = st.file_uploader(
            step.label,
            type=["csv"],
            key=f"upload_{step.state_rows}",
            label_visibility="collapsed",
        )

    if api_col is not None and step.kind == "inventory":
        with api_col:
            servers = SQL.servers()
            if not servers:
                st.button(
                    "Query server — no .env config",
                    key=f"sql_{step.state_rows}",
                    disabled=True,
                    use_container_width=True,
                    help=(
                        "Set RECONCILE_SQL_DIRECT_HOST or "
                        "RECONCILE_SQL_RI_HOST in .env to query the source "
                        "servers directly."
                    ),
                )
            else:
                choices = [s.label for s in servers]
                if len(servers) > 1:
                    choices.append("Both")
                picked = st.selectbox(
                    "Server",
                    choices,
                    index=len(choices) - 1,
                    key=f"sqlsrv_{step.state_rows}",
                    label_visibility="collapsed",
                )
                if st.button(
                    "Query server",
                    key=f"sql_{step.state_rows}",
                    use_container_width=True,
                    help=f"Runs: {INVENTORY_QUERY}",
                ):
                    wanted = (
                        servers
                        if picked == "Both"
                        else [s for s in servers if s.label == picked]
                    )
                    rows: list = []
                    failures: list[str] = []
                    for server in wanted:
                        try:
                            rows += load_inventory_from_sql(
                                server.connection_string(),
                                INVENTORY_QUERY,
                                server.label,
                            )
                        except QueryError as exc:
                            failures.append(str(exc))
                    if failures:
                        st.session_state[step.state_rows] = None
                        st.session_state.step_errors[step.state_rows] = failures[0]
                        for message in failures:
                            st.error(message)
                    else:
                        st.session_state[step.state_rows] = rows
                        st.session_state[step.state_label] = (
                            f"{picked} server" if picked != "Both" else "Direct + RI"
                        )
                        st.session_state.step_errors.pop(step.state_rows, None)
                        st.session_state.raw_text.pop(step.state_rows, None)
                        st.rerun()

    if api_col is not None and step.state_rows == "vault_rows":
        with api_col:
            if API.vault_configured:
                if st.button(
                    "Call API",
                    key=f"api_{step.state_rows}",
                    use_container_width=True,
                    help=(
                        f"GET {API.vault_url()} — matching on "
                        f"{API.vault_name_field}"
                    ),
                ):
                    try:
                        rows = load_vault_from_api(
                            API.vault_url(),
                            API.api_key,
                            name_field=API.vault_name_field,
                        )
                    except QueryError as exc:
                        st.session_state[step.state_rows] = None
                        st.session_state.step_errors[step.state_rows] = str(exc)
                        st.error(str(exc))
                    else:
                        st.session_state[step.state_rows] = rows
                        st.session_state[step.state_label] = "Data Vault API"
                        st.session_state.step_errors.pop(step.state_rows, None)
                        st.session_state.raw_text.pop(step.state_rows, None)
                        st.rerun()

            else:
                st.button(
                    "Call API — no .env config",
                    key=f"api_{step.state_rows}",
                    disabled=True,
                    use_container_width=True,
                    help=(
                        "Set RECONCILE_API_HOST and MOODYS_API_KEY in .env to "
                        "fetch the archive list directly."
                    ),
                )

    if api_col is not None and step.state_rows == "target_rows":
        with api_col:
            note = "awaiting function" if API.configured else "no .env config"
            st.button(
                f"Call API — {note}",
                key=f"api_{step.state_rows}",
                disabled=True,
                use_container_width=True,
                help=(
                    "Not yet available — awaiting the list-exposures function "
                    "(TASK-0054). Upload a CSV for now."
                    + (
                        ""
                        if API.configured
                        else f" Also unset: {', '.join(API.missing())}."
                    )
                ),
            )

    with card:
        if uploaded is None:
            st.session_state[step.state_rows] = None
            st.session_state.step_errors.pop(step.state_rows, None)
            st.session_state.raw_text.pop(step.state_rows, None)
            return

        text = uploaded.getvalue().decode("utf-8-sig")
        st.session_state.raw_text[step.state_rows] = (uploaded.name, text)

        try:
            if step.kind == "target":
                headers = target_headers(text)
                if not headers:
                    raise LoadError("that file has no header row")
                guess = guess_key_column(headers)
                chosen = st.selectbox(
                    "Match on which column?",
                    headers,
                    index=headers.index(guess) if guess in headers else 0,
                    key=f"key_{step.state_rows}",
                    help="Guessed from the headers. Confirm it — nothing else checks this.",
                )
                rows = load_target_from_csv(text, chosen)
            else:
                rows = load_source_list(text, step.kind)
            st.session_state[step.state_rows] = rows
            st.session_state[step.state_label] = uploaded.name
            st.session_state.step_errors.pop(step.state_rows, None)
        except LoadError as exc:
            st.session_state[step.state_rows] = None
            st.session_state.step_errors[step.state_rows] = str(exc)
            st.error(str(exc))

with st.sidebar:
    st.title("\U0001F50E Reconcile")
    st.caption("Did every in-scope database reach Data Vault?")

    for step in STEPS:
        render_step(step)

    st.divider()

    missing_steps = [
        s for s in STEPS if s.required and st.session_state.get(s.state_rows) is None
    ]
    ready = not missing_steps
    if ready:
        st.markdown(
            "<div class='sp-gate sp-gate-ok'>Steps 1 and 3 complete</div>",
            unsafe_allow_html=True,
        )
    else:
        waiting = " and ".join(f"{s.number}. {s.title}" for s in missing_steps)
        st.markdown(
            f"<div class='sp-gate'>Waiting on step {html.escape(waiting)}</div>",
            unsafe_allow_html=True,
        )

    if st.button(
        "Run reconciliation",
        type="primary",
        disabled=not ready,
        use_container_width=True,
    ):
        master_rows = st.session_state.master_rows
        target_rows = st.session_state.target_rows
        vault_rows = st.session_state.vault_rows
        scope_rows = st.session_state.scope_rows
        result = reconcile(master_rows, target_rows, vault_rows, scope_rows)
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
        scope_id = None
        if scope_rows is not None:
            scope_id = store.save_snapshot(
                "scope",
                st.session_state.scope_label,
                st.session_state.scope_label,
                scope_rows,
            )
        st.session_state.result = result
        st.session_state.run_id = store.save_run(
            master_id,
            target_id,
            result,
            vault_snapshot_id=vault_id,
            scope_snapshot_id=scope_id,
        )

    st.divider()
    run_count = len(store.runs())
    if SQL.any_configured:
        st.caption(
            "Source servers: " + ", ".join(s.label for s in SQL.servers())
        )
    if API.configured:
        st.caption(f"API host: `{API.host}` (list function pending)")
    else:
        st.caption(
            "API not configured — copy `.env.example` to `.env` to set the "
            "host and key."
        )
    st.caption(f"Store: `{DEFAULT_DB_PATH.name}`")
    st.caption(f"{run_count} run(s) saved")

    with st.expander("Purge old runs"):
        st.caption(
            "Every press of Reconcile stores a new run, so the file grows with "
            "clicks as much as with new data. This deletes all but the most "
            "recent runs, and the snapshots nothing else is using."
        )
        keep = st.number_input(
            "Runs to keep",
            min_value=0,
            max_value=max(run_count, 1),
            value=min(5, run_count) if run_count else 0,
            step=1,
            help="0 empties the store entirely.",
        )
        preview = store.purge_preview(int(keep))
        if preview.nothing_to_do:
            st.caption("Nothing to purge at that setting.")
        else:
            st.warning(f"Would delete {preview.summary()}. This cannot be undone.")
            confirm = st.checkbox("Yes, delete them", key="purge_confirm")
            if st.button("Purge", disabled=not confirm, use_container_width=True):
                report = store.purge(int(keep))
                if st.session_state.run_id not in report.runs_kept:
                    st.session_state.result = None
                    st.session_state.run_id = None
                st.success(f"Deleted {report.summary()}.")
                st.rerun()

st.title("Source to Target reconciliation")

result: Reconciliation | None = st.session_state.result

if result is None:
    st.info(
        "Upload the **Source (On Prem)** inventory and the **Data Bridge** list in "
        "the sidebar, then press **Reconcile**. Add the **Data Vault** list to "
        "check archiving too."
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
                        "Source": r.master_label,
                        "Data Bridge": r.target_label,
                        "Rows (src/bridge/vault)": (
                            f"{r.master_count}/{r.target_count}/"
                            f"{'—' if r.vault_count is None else r.vault_count}"
                        ),
                        "Inputs": r.fingerprint,
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
            st.session_state.outcome_filter = None
            st.rerun()

    with st.expander("What the outcomes mean"):
        for outcome, text in OUTCOME_HELP.items():
            st.markdown(f"**{outcome.value}** — {text}")
    st.stop()

counts = result.counts()

st.subheader("Summary")

headline_stages = {s.name: s for s in funnel(result)}
archived_note = (
    "reconciled in Data Vault"
    if result.vault_checked
    else "no vault list supplied — unconfirmed, not zero"
)
render_headline_cards(
    [
        (
            "SOURCE (On Prem)",
            result.master_count,
            "databases on the on-prem SQL servers",
            "",
        ),
        (
            "THE SCOPE (Total)",
            result.scope_names if result.scope_checked else result.master_count,
            "on the to-migrate list"
            if result.scope_checked
            else "no scope list supplied — every on-prem row treated as wanted",
            "",
        ),
        (
            "TARGET (Archive)",
            counts[Outcome.ARCHIVED],
            archived_note,
            "good" if result.vault_checked else "",
        ),
    ],
    primary=True,
)

archived_size = size_of(result, Outcome.ARCHIVED)
transit_size = size_of(result, Outcome.NOT_ARCHIVED)
unchecked_size = size_of(result, Outcome.IMPORTED)

def _volume_phrase(total) -> str:
    if total.complete:
        return f"{total.gigabytes:,.1f} GB"
    return f"at least {total.gigabytes:,.1f} GB"

wrong_units = next(
    (
        t
        for t in (archived_size, transit_size, unchecked_size)
        if t.units_look_wrong
    ),
    None,
)
if wrong_units is not None:
    st.warning(
        f"**`{wrong_units.field_used}` does not look like megabytes** — it "
        f"averages {wrong_units.megabytes / wrong_units.rows_with_size:,.0f} "
        "per database. That is almost certainly bytes. No volumes are shown, "
        "because they would be wrong by roughly a millionfold."
    )
else:
    parts = []
    if archived_size.rows_with_size:
        parts.append(
            f"**Data Vault {_volume_phrase(archived_size)}** "
            f"({archived_size.rows_with_size} of {archived_size.rows} archived)"
        )
    if transit_size.rows_with_size:
        parts.append(
            f"**in transit {_volume_phrase(transit_size)}** "
            f"({transit_size.rows_with_size} of {transit_size.rows})"
        )
    if unchecked_size.rows_with_size:
        parts.append(
            f"**Data Bridge {_volume_phrase(unchecked_size)}** "
            f"({unchecked_size.rows_with_size} of {unchecked_size.rows}, "
            "archiving unchecked)"
        )
    if parts:
        st.caption("Volume — " + " · ".join(parts) + ".")

render_headline_cards(
    [
        (
            "Not in Scope",
            counts[Outcome.OUT_OF_SCOPE],
            "on prem, but not on the scope list",
            "",
        ),
        (
            "In Scope, Missing",
            counts[Outcome.NOT_IN_INVENTORY],
            "wanted, but not found on prem",
            "warn" if counts[Outcome.NOT_IN_INVENTORY] else "",
        ),
        (
            "In Scope and Available",
            headline_stages["In scope"].count,
            "wanted and on prem — movable today",
            "",
        ),
        (
            "In Transit",
            counts[Outcome.NOT_ARCHIVED],
            "in Data Bridge, not yet archived",
            "warn" if counts[Outcome.NOT_ARCHIVED] else "",
        ),
    ]
)

findings = [o for o in Outcome if o in FINDINGS]
SETTLED_ORDER = (Outcome.OUT_OF_SCOPE, Outcome.IMPORTED, Outcome.ARCHIVED)
settled = [o for o in SETTLED_ORDER if o not in FINDINGS] + [
    o for o in Outcome if o not in FINDINGS and o not in SETTLED_ORDER
]

st.markdown(
    "<div class='rc-label rc-label-spaced'>Settled</div>", unsafe_allow_html=True
)
render_outcome_cards(settled, counts)

scope_note = (
    f"{result.master_count} master rows against {result.target_count} platform rows"
)
if result.vault_checked:
    scope_note += f" and {result.vault_count} vault rows"
scope_note += f". Run {st.session_state.run_id}."
st.caption(scope_note)

st.markdown(
    "<div class='rc-label rc-label-spaced'>Needs attention"
    "<span class='rc-hint'> — click to filter the results table</span></div>",
    unsafe_allow_html=True,
)
render_finding_buttons(findings, counts, len(result.findings), result.is_clean)

if result.targets_are_identical:
    st.error(
        "**The Data Bridge and Data Vault lists hold exactly the same names.** "
        "That almost always means the same file was loaded into both slots. "
        "Every imported database therefore reports as *Archived*, which is the "
        "most flattering possible answer and the least likely to be true — "
        "check the two uploads before reading anything below."
    )

if not result.vault_checked:
    st.warning(
        "**No Data Vault list supplied**, so nothing here confirms archiving. "
        "Rows show as *Archiving unchecked*, which is not the same as done — "
        "archiving is a separate, explicit step that is not automatic on import."
    )

st.divider()

SIDE_TABS = (
    ("master", "Source"),
    ("scope_rows", "Scope"),
    ("target_rows", "Data Bridge"),
    ("vault_rows", "Target"),
)
loaded_sides = [
    (key, title) for key, title in SIDE_TABS if key in st.session_state.raw_text
]

tabs = st.tabs(
    ["Results", *(f"{title} file" for _, title in loaded_sides), "Report"]
)
results_tab = tabs[0]
report_tab = tabs[-1]

with report_tab:
    st.caption(
        "How far the estate has got. Each step counts the databases that "
        "reached it, so the drop between two steps is the work outstanding "
        "there."
    )
    stages = funnel(result)
    widest = max((s.count for s in stages), default=0) or 1

    scope_base = next(
        (s.count for s in stages if s.name == "In scope"), 0
    )
    rows_html = []
    for stage in stages:
        pct = 100.0 * stage.count / widest
        share = (
            f"{100.0 * stage.count / scope_base:.0f}% of scope"
            if scope_base and stage.name not in ("On Prem", "In scope")
            else ""
        )
        lost = (
            f"−{stage.lost}"
            if stage.lost
            else ""
        )
        if stage.lost and stage.of_previous:
            lost += f"<span class='fn-kept'>{stage.percent_of_previous:.0f}% kept</span>"
        done = stage.name == "Data Vault" and result.vault_checked
        bar_class = "fn-bar fn-bar-done" if done else "fn-bar"
        rows_html.append(
            "<div class='fn-row'>"
            f"<div class='fn-stage'>{html.escape(stage.name)}</div>"
            "<div class='fn-track'>"
            f"<div class='{bar_class}' style='width:{pct:.1f}%'></div>"
            f"<div class='fn-value'>{stage.count}"
            f"<span class='fn-share'>{share}</span></div>"
            "</div>"
            f"<div class='fn-drop'>{lost}</div>"
            f"<div class='fn-note'>{html.escape(stage.note)}</div>"
            "</div>"
        )
    st.markdown(
        f"<div class='fn'>{''.join(rows_html)}</div>", unsafe_allow_html=True
    )

    funnel_frame = pd.DataFrame(
        [
            {
                "Stage": s.name,
                "Databases": s.count,
                "Lost here": s.lost,
                "% of previous": round(s.percent_of_previous, 1),
                "Note": s.note,
            }
            for s in stages
        ]
    )
    with st.expander("The same figures as a table"):
        st.dataframe(funnel_frame, hide_index=True, use_container_width=True)

    if result.vault_checked:
        archived = stages[-1].count
        started = stages[2].count
        st.caption(
            f"**{archived} of {started}** available databases have reached Data Vault. "
            "Only Archived means done — a database in Data Bridge but not in "
            "Data Vault is in transit, not migrated."
        )
    else:
        st.info(
            "The Vault stage reads zero because **no Data Vault list was "
            "supplied** — archiving is unconfirmed, not absent. Load one to "
            "complete the funnel."
        )

    st.download_button(
        "Download this report (CSV)",
        data=funnel_to_csv(result),
        file_name="reconciliation-report.csv",
        mime="text/csv",
    )

    st.divider()
    st.subheader("Earlier runs")

    history = store.runs()
    if len(history) < 2:
        st.caption(
            "Only this run so far. Reconcile again and earlier runs can be "
            "reopened and compared here."
        )
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Run": r.id,
                        "Ran at (UTC)": r.ran_at,
                        "Source": r.master_label,
                        "Data Bridge": r.target_label,
                        "Rows (src/bridge/vault)": (
                            f"{r.master_count}/{r.target_count}/"
                            f"{'—' if r.vault_count is None else r.vault_count}"
                        ),
                        "Inputs": r.fingerprint,
                        "Viewing": "←" if r.id == st.session_state.run_id else "",
                    }
                    for r in history
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )

        run_ids = [r.id for r in history]
        reopen_col, compare_col = st.columns(2)

        with reopen_col:
            st.markdown("**Reopen a run**")
            chosen_run = st.selectbox(
                "Run to reopen",
                run_ids,
                index=run_ids.index(st.session_state.run_id)
                if st.session_state.run_id in run_ids
                else 0,
                key="reopen_run",
                label_visibility="collapsed",
            )
            if st.button("Reopen", use_container_width=True):
                st.session_state.result = store.load_run(int(chosen_run))
                st.session_state.run_id = int(chosen_run)
                st.session_state.outcome_filter = None
                st.rerun()

        with compare_col:
            st.markdown("**Compare two runs**")
            earlier = st.selectbox(
                "Earlier run",
                run_ids,
                index=min(1, len(run_ids) - 1),
                key="cmp_earlier",
            )
            later = st.selectbox(
                "Later run", run_ids, index=0, key="cmp_later"
            )
            if st.button("Compare", use_container_width=True):
                if earlier == later:
                    st.info("Pick two different runs.")
                else:
                    diff = store.compare_runs(int(earlier), int(later))
                    changed, appeared, gone = st.columns(3)
                    with changed:
                        st.markdown("**Changed outcome**")
                        st.write(diff["changed"] or "—")
                    with appeared:
                        st.markdown("**Newly appeared**")
                        st.write(diff["appeared"] or "—")
                    with gone:
                        st.markdown("**No longer present**")
                        st.write(diff["disappeared"] or "—")

with results_tab:
    frame = results_frame(result)

    clicked = st.session_state.outcome_filter
    if clicked:
        st.info(f"Filtered to **{clicked}** — press *Show all* above to clear.")

    left, right = st.columns([2, 1])
    with left:
        default = (
            [clicked] if clicked else [o.value for o in Outcome if counts[o]]
        )
        chosen_outcomes = st.multiselect(
            "Outcome",
            [o.value for o in Outcome],
            default=default,
            key=f"outcome_multiselect_{clicked or 'all'}",
            help="Findings are pre-selected. Clicking a card above filters to it.",
        )
    with right:
        search = st.text_input("Search name", "")

    view = frame[frame["Outcome"].isin(chosen_outcomes)] if chosen_outcomes else frame
    if search:
        view = view[view["Name"].str.contains(search, case=False, na=False)]

    st.dataframe(view, hide_index=True, use_container_width=True)
    st.caption(f"Showing {len(view)} of {len(frame)} rows.")

    left_dl, right_dl = st.columns(2)
    with left_dl:
        st.download_button(
            f"Download these {len(view)} rows (CSV)",
            data=view.to_csv(index=False),
            file_name="reconciliation-filtered.csv",
            mime="text/csv",
            use_container_width=True,
            disabled=len(view) == len(frame),
            help="What the table is showing, with the filter applied.",
        )
    with right_dl:
        st.download_button(
            f"Download all {len(frame)} rows (CSV)",
            data=to_csv(result),
            file_name="reconciliation.csv",
            mime="text/csv",
            use_container_width=True,
            help="Every row, whatever the filter says.",
        )

for tab, (key, title) in zip(tabs[1:-1], loaded_sides, strict=True):
    with tab:
        filename, text = st.session_state.raw_text[key]
        render_file_tab(title, filename, text, key)
