from __future__ import annotations

import html
import io
import warnings
from datetime import date
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import streamlit as st
from reconcile import (
    FINDINGS,
    AUTOMATED_ACTORS,
    CREATED_DATE_COLUMNS,
    CREATOR_COLUMNS,
    MASTER_NAME_COLUMNS,
    SIZE_FIELDS_MB,
    SIZE_UNITS_TO_MB,
    SizeMatch,
    SizeTotal,
    apply_size_reference,
    load_size_reference,
    size_columns_guess,
    size_unit_guess,
    LoadError,
    Outcome,
    QueryError,
    Reconciliation,
    distinguishing_values,
    duplicate_groups,
    funnel,
    funnel_to_csv,
    guess_key_column,
    match_key,
    load_bridge_from_api,
    load_inventory_from_sql,
    load_source_list,
    load_target_from_csv,
    load_vault_from_api,
    ARCHIVE_TIME_FIELDS,
    compare_to_newest,
    newest_first,
    reconcile,
    SERVER_FIELD,
    rows_per_server,
    size_of,
    snapshot_size,
    target_headers,
    to_csv,
)
from settings import (
    INVENTORY_QUERY,
    config_conflicts,
    completion_date,
    load_settings,
    load_sql_settings,
)
from store import DEFAULT_DB_PATH, Store
from visual_style import semantic_colour, stylesheet

API = load_settings()
SQL = load_sql_settings()

st.set_page_config(page_title="Reconcile", page_icon="\U0001F50E", layout="wide")

st.markdown(
    """
    <style>
      .rc-row { display:flex; flex-wrap:wrap; gap:.6rem; margin:.15rem 0 .1rem; }
      .rc-card {
        flex:1 1 130px; min-width:130px; padding:.35rem .6rem;
        border:1px solid color-mix(in srgb, var(--rc) 35%, transparent);
        border-left:4px solid var(--rc);
        border-radius:6px;
        background:color-mix(in srgb, var(--rc) 7%, transparent);
      }
      .rc-card-empty { opacity:.45; }
      .rc-count {
        color:var(--rc); font-size:1.4rem; font-weight:700; line-height:1.1;
        font-variant-numeric:tabular-nums;
      }
      .rc-name {
        color:var(--rc); font-weight:600; font-size:.85rem;
        white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
      }
      .rc-label {
        font-size:.75rem; font-weight:600; letter-spacing:.06em;
        text-transform:uppercase; opacity:.6; margin-bottom:.15rem;
      }
      .rc-label-spaced { margin-top:.5rem; }
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
        display:flex; flex-wrap:wrap; gap:.6rem; margin:.15rem 0 .5rem;
      }

      .hl-row-primary { margin-bottom:.55rem; }
      .hl-card {

        flex-grow:1; flex-shrink:1; min-width:150px; padding:.5rem .7rem;
        border:1px solid rgba(128,128,128,.28); border-radius:8px;
        background:rgba(128,128,128,.06);
      }
      .hl-count {
        font-size:1.5rem; font-weight:700; line-height:1.05;
        font-variant-numeric:tabular-nums;
      }

      .hl-figure { display:flex; align-items:baseline; gap:.5rem; flex-wrap:wrap; }

      .hl-split {
        font-size:1rem; font-weight:600; opacity:.7;
        font-variant-numeric:tabular-nums;
      }

      .hl-row-primary .hl-card {
        padding:.65rem .85rem; background:rgba(128,128,128,.1);
        border-color:rgba(128,128,128,.38);
      }
      .hl-row-primary .hl-count { font-size:2.1rem; }
      .hl-row-primary .hl-split { font-size:1.05rem; }
      .hl-row-primary .hl-name { font-size:.95rem; letter-spacing:.01em; }

      .hl-good { color:#1e7b34; }
      .hl-warn { color:#b26a00; }
      .hl-bad  { color:#b3261e; }
      .hl-name {
        font-weight:600; font-size:.88rem; margin-top:.05rem; line-height:1.2;
      }
      .hl-note { font-size:.74rem; opacity:.6; margin-top:.15rem; line-height:1.25; }

      .dl { text-align:right; padding-top:1.1rem; line-height:1.35; }
      .dl-today { font-size:.95rem; font-weight:600; }
      .dl-left {
        font-size:1.35rem; font-weight:700; font-variant-numeric:tabular-nums;
      }
      .dl-target { font-size:.75rem; opacity:.6; }

      .dl-warn { color:#b26a00; }
      .dl-bad { color:#b3261e; }

      .hl-volume {
        font-size:.86rem; font-weight:600; opacity:.78; margin-top:.25rem;
        font-variant-numeric:tabular-nums;
      }

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

      .fn-seg { position:absolute; top:0; bottom:0; }
      .fn-seg-done { background:#2f6f4f; border-radius:4px 0 0 4px; }
      .fn-seg-transit { background:#d98324; }
      .fn-seg-only { border-radius:4px; }

      .fn-seg-scope { background:#3a6ea5; border-radius:4px 0 0 4px; }
      .fn-seg-absent {
        background:repeating-linear-gradient(
          135deg, #c4342c, #c4342c 5px, #9b241d 5px, #9b241d 10px
        );
        border-radius:0 4px 4px 0;
      }

      .fn-gap {
        position:absolute; top:0; bottom:0;
        display:flex; align-items:center;
        padding-left:.5rem; font-size:.8rem; font-weight:600;
        color:#b3261e; opacity:.85; white-space:nowrap; pointer-events:none;
      }
      .fn-gap-kept { font-weight:400; opacity:.75; margin-left:.4rem; }
      .fn-legend {
        display:flex; gap:.9rem; flex-wrap:wrap;
        font-size:.75rem; opacity:.75; margin:-.2rem 0 .8rem 8.25rem;
      }
      .fn-key { display:inline-block; width:.6rem; height:.6rem;
                border-radius:2px; margin-right:.3rem; vertical-align:middle; }

      .fn-value {
        position:relative; margin-left:auto; padding-right:.6rem;
        font-weight:700; font-size:.95rem;
      }

      .fn-value-in {
        position:absolute; left:.7rem; top:0; bottom:0;
        display:flex; align-items:center;
        color:#fff; font-weight:700; font-size:.95rem;
        text-shadow:0 1px 2px rgba(0,0,0,.35);
        pointer-events:none; white-space:nowrap;
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

MINUS = "\u2212"

st.markdown(stylesheet(st.context.theme.type), unsafe_allow_html=True)
RED = semantic_colour("red")
AMBER = semantic_colour("orange")
GREEN = semantic_colour("green")
GREY = semantic_colour("muted")

OUTCOME_COLOUR: dict[Outcome, str] = {
    Outcome.MISSING: RED,
    Outcome.DUPLICATE: RED,
    Outcome.NOT_ARCHIVED: AMBER,
    Outcome.NOT_IN_INVENTORY: AMBER,
    Outcome.ORPHANED: AMBER,
    Outcome.ARCHIVED: GREEN,
    Outcome.OUT_OF_SCOPE: GREY,
    Outcome.IMPORTED: GREY,
    Outcome.IMPORT_UNCHECKED: GREY,
}

OUTCOME_HELP: dict[Outcome, str] = {
    Outcome.MISSING: (
        "In scope and on prem, but in neither Data Bridge nor Data Vault. "
        "Work not started."
    ),
    Outcome.NOT_ARCHIVED: (
        "Still on Data Bridge, not yet in Data Vault. Archiving moves a "
        "database rather than copying it, so the bridge is transient and "
        "should be empty when the migration is done — this count is the "
        "backlog still to archive."
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
    Outcome.IMPORT_UNCHECKED: (
        "Not in Data Vault, and no Data Bridge list was supplied — so whether "
        "it ever imported is unknown. Not the same as Missing, which would "
        "claim it never reached the platform. Load step 3 to find out."
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
    st.session_state.setdefault("step_source", {})
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
    cards: list[tuple[str, int, str, str] | tuple[str, int, str, str, str]],
    *,
    primary: bool = False,
) -> None:
    basis = 100 / max(len(cards), 1)
    row_class = "hl-row hl-row-primary" if primary else "hl-row"
    html_cards = []
    for card in cards:
        title, count, note, tone = card[:4]
        volume = card[4] if len(card) > 4 else ""
        tone_class = f" hl-{tone}" if tone else ""
        volume_html = (
            f"<div class='hl-volume'>{html.escape(volume)}</div>"
            if volume
            else ""
        )
        breakdown = card[5] if len(card) > 5 else ""
        breakdown_html = (
            f"<span class='hl-split'>{html.escape(breakdown)}</span>"
            if breakdown
            else ""
        )
        html_cards.append(
            f"<div class='hl-card' style='flex-basis:calc({basis:.4f}% - .9rem)'>"
            f"<div class='hl-figure'>"
            f"<span class='hl-count{tone_class}'>{count}</span>"
            f"{breakdown_html}"
            f"</div>"
            f"<div class='hl-name'>{html.escape(title)}</div>"
            f"{volume_html}"
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
            width="stretch",
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
                width="stretch",
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

def _parse_stamps(values: pd.Series) -> pd.Series:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Could not infer format")
        return pd.to_datetime(values, errors="coerce", utc=True)

def rows_to_frame(fetched: object) -> pd.DataFrame:
    if isinstance(fetched, pd.DataFrame):
        return fetched
    return pd.DataFrame(
        [
            {k: v for k, v in row.data.items() if k != "_key_column"}
            for row in (fetched or [])
        ]
    ).fillna("")

def _first_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    def _key(name: str) -> str:
        return name.strip().casefold().replace(" ", "").replace("_", "")

    normalised = {_key(c): c for c in frame.columns}
    for candidate in candidates:
        if _key(candidate) in normalised:
            return normalised[_key(candidate)]
    return None

def render_file_tab(
    title: str,
    filename: str,
    text: str | None,
    key: str,
    fetched: list | None = None,
) -> None:
    if text is not None:
        rows = pd.read_csv(io.StringIO(text), dtype=str).fillna("")
    else:
        rows = pd.DataFrame(
            [
                {k: v for k, v in row.data.items() if k != "_key_column"}
                for row in (fetched or [])
            ]
        ).fillna("")

    left, mid, right = st.columns(3)
    left.metric("Rows", len(rows))
    mid.metric("Columns", len(rows.columns))
    matched_on = ""
    if key != "master_rows":
        stored = st.session_state.get(key) or []
        if stored:
            matched_on = stored[0].data.get("_key_column", "")
    right.metric("Matching on", matched_on or "—")

    if text is not None:
        st.caption(f"`{filename}`")
    else:
        st.caption(
            f"{filename} — fetched live, not from a file. Every column the "
            "source returned."
        )

    scope_view = "All"
    if key == "scope_rows" and st.session_state.result is not None:
        scope_view = st.radio(
            "Show",
            ("All", "Missing from prem", "Available on prem"),
            horizontal=True,
            key=f"scope_view_{key}",
            help=(
                "Missing from prem is the In Scope, Missing figure on the "
                "summary: requested, but absent from the on-prem inventory."
            ),
        )

    creator_column = _first_column(rows, CREATOR_COLUMNS)
    date_column = _first_column(rows, CREATED_DATE_COLUMNS)

    creators: list[str] = []
    date_range = None
    if creator_column is not None or date_column is not None:
        left_f, right_f = st.columns(2)
        if creator_column is not None:
            with left_f:
                options = sorted(
                    {
                        value.strip()
                        for value in rows[creator_column].astype(str)
                        if value.strip() and value.strip().lower() != "nan"
                    }
                )
                creators = st.multiselect(
                    f"Created by ({creator_column})",
                    options,
                    key=f"creator_{key}",
                    placeholder="anyone",
                )
        if date_column is not None:
            with right_f:
                stamps = _parse_stamps(rows[date_column])
                if stamps.notna().any():
                    earliest = stamps.min().date()
                    latest = stamps.max().date()
                    date_range = st.date_input(
                        f"Created between ({date_column})",
                        value=(earliest, latest),
                        min_value=earliest,
                        max_value=latest,
                        key=f"created_{key}",
                    )

    search = st.text_input(
        "Filter rows containing", "", key=f"filter_{key}", placeholder="any text"
    )
    view = rows
    if scope_view != "All":
        wanted = {
            row.name.strip().casefold()
            for row in st.session_state.result.rows
            if row.outcome is Outcome.NOT_IN_INVENTORY
        }
        name_column = next(
            (c for c in rows.columns if c.strip().casefold() in MASTER_NAME_COLUMNS),
            rows.columns[0] if len(rows.columns) else None,
        )
        if name_column is not None:
            in_missing = rows[name_column].astype(str).str.strip().str.casefold().isin(
                wanted
            )
            view = rows[in_missing if scope_view == "Missing from prem" else ~in_missing]

    if creators and creator_column is not None:
        view = view[view[creator_column].astype(str).str.strip().isin(creators)]

    if (
        date_range
        and date_column is not None
        and isinstance(date_range, (tuple, list))
        and len(date_range) == 2
    ):
        start, end = date_range
        stamps = _parse_stamps(view[date_column])
        within = (stamps.dt.date >= start) & (stamps.dt.date <= end)
        view = view[within | stamps.isna()]

    if search:
        mask = rows.apply(
            lambda r: r.astype(str).str.contains(search, case=False, na=False).any(),
            axis=1,
        )
        view = view[mask.loc[view.index]]

    st.dataframe(view, hide_index=True, width="stretch", height=420)
    st.caption(f"Showing {len(view)} of {len(rows)} rows, every column as supplied.")

    left_dl, right_dl = st.columns(2)
    with left_dl:
        slug = (
            scope_view.lower().replace(" ", "-")
            if scope_view != "All"
            else "filtered"
        )
        st.download_button(
            f"Download these {len(view)} rows (CSV)",
            data=view.to_csv(index=False),
            file_name=f"{Path(filename).stem}-{slug}.csv",
            mime="text/csv",
            key=f"dl_filtered_{key}",
            width="stretch",
            disabled=len(view) == len(rows),
            help="What the table is showing, with every filter applied.",
        )
    with right_dl:
        if text is not None:
            st.download_button(
                "Download the file as supplied",
                data=text,
                file_name=filename,
                mime="text/csv",
                key=f"dl_raw_{key}",
                width="stretch",
                help="The original upload, byte for byte.",
            )
        else:
            st.download_button(
                f"Download all {len(rows)} rows (CSV)",
                data=rows.to_csv(index=False),
                file_name=filename,
                mime="text/csv",
                key=f"dl_raw_{key}",
                width="stretch",
                help="Everything returned, whatever the filter shows.",
            )

    if text is not None:
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
        False, "target", "target_rows", "target_label",
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

    uploaded = None
    if step.state_rows not in ("target_rows", "vault_rows"):
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
                    width="stretch",
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
                    width="stretch",
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
                        st.session_state.step_source[step.state_rows] = "sql"
                        st.session_state.step_errors.pop(step.state_rows, None)
                        st.session_state.raw_text.pop(step.state_rows, None)
                        st.rerun()

    if api_col is not None and step.state_rows == "vault_rows":
        with api_col:
            if API.vault_configured:
                if st.button(
                    "Call API",
                    key=f"api_{step.state_rows}",
                    width="stretch",
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
                        st.session_state.step_source[step.state_rows] = "api"
                        st.session_state.step_errors.pop(step.state_rows, None)
                        st.session_state.raw_text.pop(step.state_rows, None)
                        st.rerun()

            else:
                st.button(
                    "Call API — no .env config",
                    key=f"api_{step.state_rows}",
                    disabled=True,
                    width="stretch",
                    help=(
                        "Set RECONCILE_API_HOST and MOODYS_API_KEY in .env to "
                        "fetch the archive list directly."
                    ),
                )

    if api_col is not None and step.state_rows == "target_rows":
        with api_col:
            if API.configured:
                if st.button(
                    "Call API",
                    key=f"api_{step.state_rows}",
                    width="stretch",
                    help=(
                        f"GET {API.exposures_url()} — matching on "
                        f"{API.bridge_name_field}"
                    ),
                ):
                    try:
                        rows = load_bridge_from_api(
                            API.exposures_url(),
                            API.api_key,
                            name_field=API.bridge_name_field,
                        )
                    except QueryError as exc:
                        st.session_state[step.state_rows] = None
                        st.session_state.step_errors[step.state_rows] = str(exc)
                        st.error(str(exc))
                    else:
                        st.session_state[step.state_rows] = rows
                        st.session_state[step.state_label] = "Data Bridge API"
                        st.session_state.step_source[step.state_rows] = "api"
                        st.session_state.step_errors.pop(step.state_rows, None)
                        st.session_state.raw_text.pop(step.state_rows, None)
                        st.rerun()
            else:
                st.button(
                    "Call API — no .env config",
                    key=f"api_{step.state_rows}",
                    disabled=True,
                    width="stretch",
                    help=(
                        "Set RECONCILE_API_HOST and MOODYS_API_KEY in .env to "
                        f"fetch the platform list directly. Unset: "
                        f"{', '.join(API.missing())}."
                    ),
                )

    with card:
        if uploaded is None:
            if st.session_state.step_source.get(step.state_rows) == "upload":
                st.session_state[step.state_rows] = None
                st.session_state.step_source.pop(step.state_rows, None)
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
            st.session_state.step_source[step.state_rows] = "upload"
            st.session_state.step_errors.pop(step.state_rows, None)
        except LoadError as exc:
            st.session_state[step.state_rows] = None
            st.session_state.step_errors[step.state_rows] = str(exc)
            st.error(str(exc))

def refresh_all_sources() -> tuple[list[str], list[str]]:
    loaded: list[str] = []
    failures: list[str] = []

    servers = SQL.servers()
    if not servers:
        failures.append("Source: no SQL server configured in .env")
    else:
        rows: list = []
        server_errors: list[str] = []
        for server in servers:
            try:
                rows += load_inventory_from_sql(
                    server.connection_string(), INVENTORY_QUERY, server.label
                )
            except QueryError as exc:
                server_errors.append(f"Source ({server.label}): {exc}")
        if server_errors:
            failures.extend(server_errors)
        else:
            st.session_state["master_rows"] = rows
            st.session_state["master_label"] = (
                "Direct + RI" if len(servers) > 1 else f"{servers[0].label} server"
            )
            st.session_state.step_source["master_rows"] = "api"
            st.session_state.step_errors.pop("master_rows", None)
            st.session_state.raw_text.pop("master_rows", None)
            loaded.append(f"Source ({len(rows):,})")

    if not API.configured:
        failures.append("Data Bridge and Data Vault: no API config in .env")
        return loaded, failures

    try:
        bridge_rows = load_bridge_from_api(
            API.exposures_url(), API.api_key, name_field=API.bridge_name_field
        )
    except QueryError as exc:
        failures.append(f"Data Bridge: {exc}")
    else:
        st.session_state["target_rows"] = bridge_rows
        st.session_state["target_label"] = "Data Bridge API"
        st.session_state.step_source["target_rows"] = "api"
        st.session_state.step_errors.pop("target_rows", None)
        st.session_state.raw_text.pop("target_rows", None)
        loaded.append(f"Data Bridge ({len(bridge_rows):,})")

    try:
        vault_rows = load_vault_from_api(
            API.vault_url(), API.api_key, name_field=API.vault_name_field
        )
    except QueryError as exc:
        failures.append(f"Data Vault: {exc}")
    else:
        st.session_state["vault_rows"] = vault_rows
        st.session_state["vault_label"] = "Data Vault API"
        st.session_state.step_source["vault_rows"] = "api"
        st.session_state.step_errors.pop("vault_rows", None)
        st.session_state.raw_text.pop("vault_rows", None)
        loaded.append(f"Data Vault ({len(vault_rows):,})")

    return loaded, failures

def _with_reference_sizes(rows: list) -> tuple[list, SizeMatch | None]:
    reference = store.latest_size_reference()
    if reference is None or not rows:
        return rows, None
    info, entries = reference
    return apply_size_reference(rows, entries, info.as_of)

def render_size_reference() -> None:
    with st.expander("Reference: database sizes", expanded=False):
        current = store.latest_size_reference()
        if current is None:
            st.caption(
                "None loaded. Load MS Amlin's size snapshot to give each "
                "on-prem database a size."
            )
        else:
            info, _ = current
            st.caption(
                f"Applied: **{html.escape(info.label)}** -- {info.row_count:,} "
                f"databases, sizes as of **{info.as_of}** (loaded "
                f"{info.loaded_at[:16].replace('T', ' ')} UTC)."
            )
        match = st.session_state.get("size_match")
        if match is not None:
            st.caption(
                f"Last run: {match.matched:,} of {match.live:,} on-prem "
                f"databases got a size; {match.unmatched:,} not in the snapshot "
                "(likely newer)"
                + (f"; {match.ambiguous:,} ambiguous across servers" if match.ambiguous else "")
                + (
                    f"; {match.not_live:,} in the snapshot but no longer on prem"
                    if match.not_live
                    else ""
                )
                + "."
            )

        uploaded = st.file_uploader(
            "Size snapshot (CSV)", type=["csv"], key="size_reference_upload"
        )
        if uploaded is None:
            return
        text = uploaded.getvalue().decode("utf-8-sig", errors="replace")
        headers = target_headers(text)
        if not headers:
            st.error("That file has no header row.")
            return
        name_guess, size_guess, server_guess = size_columns_guess(headers)

        def at(guess: str | None, options: list[str]) -> int:
            return options.index(guess) if guess in options else 0

        name_col = st.selectbox(
            "Database name column", headers, index=at(name_guess, headers), key="size_ref_name"
        )
        size_col = st.selectbox(
            "Size column", headers, index=at(size_guess, headers), key="size_ref_size"
        )
        server_options = ["(none)", *headers]
        server_col = st.selectbox(
            "Server column (optional)",
            server_options,
            index=at(server_guess, server_options),
            key="size_ref_server",
            help="Only used to tell apart the same database name on two servers.",
        )
        units = list(SIZE_UNITS_TO_MB)
        unit = st.radio(
            "Unit of the size column",
            units,
            index=units.index(size_unit_guess(size_col)),
            horizontal=True,
            key=f"size_ref_unit_{size_col}",
        )
        as_of = st.date_input("Sizes as of", value=date.today(), key="size_ref_as_of")
        label = st.text_input("Label", value=uploaded.name, key="size_ref_label")

        try:
            loaded = load_size_reference(
                text,
                name_column=name_col,
                size_column=size_col,
                unit=str(unit),
                server_column=None if server_col == "(none)" else server_col,
            )
        except LoadError as exc:
            st.error(str(exc))
            return
        total_tb = sum(e.size_mb for e in loaded.entries) / (1024 * 1024)
        st.caption(
            f"{len(loaded.entries):,} databases read, {total_tb:,.2f} TB in total"
            + (
                f"; {loaded.skipped:,} row(s) skipped -- no name, or a size that "
                "is not a number"
                if loaded.skipped
                else ""
            )
            + ". Check the total looks right for the unit chosen."
        )
        if st.button(
            "Save as reference",
            type="primary",
            disabled=not loaded.entries,
            key="size_ref_save",
            width="stretch",
        ):
            store.save_size_reference(
                label=label.strip() or uploaded.name,
                source=uploaded.name,
                as_of=as_of.isoformat(),
                entries=loaded.entries,
            )
            st.success(
                f"Saved {len(loaded.entries):,} sizes. They apply from the next "
                "Run reconciliation."
            )

with st.sidebar:
    st.title("\U0001F50E Reconcile")
    st.caption("Did every in-scope database reach Data Vault?")

    if st.button(
        "\u21bb  Refresh all",
        key="refresh_all",
        width="stretch",
        help=(
            "Runs steps 1, 3 and 4: the on-prem inventory, Data Bridge and "
            "Data Vault. Scope is a file and is left alone."
        ),
    ):
        with st.spinner("Fetching Source, Data Bridge and Data Vault..."):
            loaded, failures = refresh_all_sources()
        st.session_state["refresh_report"] = (loaded, failures)
        st.rerun()

    report = st.session_state.pop("refresh_report", None)
    if report is not None:
        refreshed, problems = report
        if refreshed:
            st.success("Refreshed: " + ", ".join(refreshed))
        for problem in problems:
            st.error(problem)

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
        width="stretch",
    ):
        master_rows, size_match = _with_reference_sizes(st.session_state.master_rows)
        st.session_state.master_rows = master_rows
        st.session_state.size_match = size_match
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
        target_id = None
        if target_rows is not None:
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

    for step in STEPS:
        render_step(step)

    render_size_reference()

    st.divider()
    run_count = len(store.runs())
    if SQL.any_configured:
        st.caption(
            "Source servers: " + ", ".join(s.label for s in SQL.servers())
        )
    if API.configured:
        st.caption(f"API host: `{API.host}`")
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
            if st.button("Purge", disabled=not confirm, width="stretch"):
                report = store.purge(int(keep))
                if st.session_state.run_id not in report.runs_kept:
                    st.session_state.result = None
                    st.session_state.run_id = None
                st.success(f"Deleted {report.summary()}.")
                st.rerun()

_title_col, _deadline_col = st.columns([3, 1])
with _title_col:
    st.title("Source to Target reconciliation")
with _deadline_col:
    _today = date.today()
    _target = completion_date()
    _days = (_target - _today).days
    if _days > 0:
        _left = f"{_days:,} day{'s' if _days != 1 else ''} left"
        _tone = "dl-warn" if _days <= 30 else ""
    elif _days == 0:
        _left, _tone = "due today", "dl-bad"
    else:
        _left, _tone = f"{abs(_days):,} days overdue", "dl-bad"
    st.markdown(
        f"<div class='dl'>"
        f"<div class='dl-today'>{_today:%d %B %Y}</div>"
        f"<div class='dl-left {_tone}'>{_left}</div>"
        f"<div class='dl-target'>target {_target:%d %B %Y}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

result: Reconciliation | None = st.session_state.result

if result is None:
    st.info(
        "Press **Refresh all** in the sidebar to fetch the Source inventory, "
        "Data Bridge and Data Vault, then **Run reconciliation**. Source can "
        "also be uploaded as a CSV if the servers are unreachable from here, "
        "and the optional **Scope** list says which databases were wanted."
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
            width="stretch",
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

archived_size = size_of(result, Outcome.ARCHIVED)

_sized_inventory, _ = _with_reference_sizes(list(st.session_state.master_rows or []))
_out_of_scope_keys = {match_key(row.name) for row in result.of(Outcome.OUT_OF_SCOPE)}
source_size = snapshot_size(_sized_inventory)
not_in_scope_size = snapshot_size(
    r for r in _sized_inventory if r.key in _out_of_scope_keys
)
available_size = snapshot_size(
    r for r in _sized_inventory if r.key not in _out_of_scope_keys
)
scope_size = SizeTotal(
    megabytes=available_size.megabytes,
    rows=available_size.rows + counts[Outcome.NOT_IN_INVENTORY],
    rows_with_size=available_size.rows_with_size,
    field_used=available_size.field_used,
)
transit_size = size_of(result, Outcome.NOT_ARCHIVED)
unchecked_size = size_of(result, Outcome.IMPORTED)

def _volume_phrase(total) -> str:
    if total.complete:
        return f"{total.gigabytes:,.1f} GB"
    return f"at least {total.gigabytes:,.1f} GB"

archived_note = (
    "reconciled in Data Vault"
    if result.vault_checked
    else "no vault list supplied — unconfirmed, not zero"
)

def _card_volume(total) -> str:
    if total.units_look_wrong or not total.rows_with_size:
        return ""
    return _volume_phrase(total)
def _short_stamp(row) -> str:
    for field_name in ARCHIVE_TIME_FIELDS:
        value = str(row.data.get(field_name) or "").strip()
        if value:
            return value[:10]
    return "—"

def _source_breakdown() -> str:
    rows = st.session_state.master_rows or []
    parts = rows_per_server(rows)
    if len(parts) < 2 or any(not label for label, _ in parts):
        return ""
    return " + ".join(f"{label} {count:,}" for label, count in parts)

def _archived_share() -> str:
    archived = counts[Outcome.ARCHIVED]
    if not archived:
        return ""
    source_total = result.master_count
    scope_total = (
        result.scope_names if result.scope_checked else result.master_count
    )
    parts = []
    if scope_total and result.scope_checked:
        parts.append(f"{100 * archived / scope_total:.1f}% of scope")
    if source_total:
        parts.append(f"{100 * archived / source_total:.1f}% of source")
    return " · ".join(parts)

render_headline_cards(
    [
        (
            "SOURCE (On Prem)",
            result.master_count,
            "databases on the on-prem SQL servers",
            "",
            _card_volume(source_size),
            _source_breakdown(),
        ),
        (
            "THE SCOPE (Total)",
            result.scope_names if result.scope_checked else result.master_count,
            "on the to-migrate list"
            if result.scope_checked
            else "no scope list supplied — every on-prem row treated as wanted",
            "",
            _card_volume(scope_size),
        ),
        (
            "TARGET (Archive)",
            counts[Outcome.ARCHIVED],
            archived_note,
            "good" if result.vault_checked else "",
            _card_volume(archived_size),
            _archived_share(),
        ),
    ],
    primary=True,
)

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
            _card_volume(not_in_scope_size),
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
            _card_volume(available_size),
        ),
        (
            "In Transit",
            counts[Outcome.NOT_ARCHIVED],
            (
                "still on the bridge — should reach zero"
                if counts[Outcome.NOT_ARCHIVED]
                else "bridge clear — nothing awaiting archive"
                if result.vault_checked
                else "no Data Vault list — nothing checked"
            ),
            (
                "warn"
                if counts[Outcome.NOT_ARCHIVED]
                else "good"
                if result.vault_checked
                else ""
            ),
            _card_volume(transit_size),
        ),
    ]
)

findings = [o for o in Outcome if o in FINDINGS]
SETTLED_ORDER = (
    Outcome.OUT_OF_SCOPE,
    Outcome.IMPORT_UNCHECKED,
    Outcome.IMPORTED,
    Outcome.ARCHIVED,
)
settled = [o for o in SETTLED_ORDER if o not in FINDINGS] + [
    o for o in Outcome if o not in FINDINGS and o not in SETTLED_ORDER
]

st.markdown(
    "<div class='rc-label rc-label-spaced'>Settled"
    "<span class='rc-hint'> &mdash; nothing to do about these</span></div>",
    unsafe_allow_html=True,
)
render_outcome_cards(settled, counts)
st.caption(
    "Each database counts once here: either it is settled (out of scope, or "
    "archived and done) or it needs attention below. Nothing appears in both."
)

scope_note = (
    f"Compared {result.master_count:,} on-prem databases against "
    f"{result.target_count:,} rows from the Data Bridge API"
)
if result.vault_checked:
    scope_note += f" and {result.vault_count:,} from the Data Vault API"
scope_note += f". Run {st.session_state.run_id}."
st.caption(scope_note)

if result.vault_checked and result.vault_count > result.target_count:
    st.caption(
        f"The Data Vault list is larger than the Data Bridge list "
        f"({result.vault_count:,} vs {result.target_count:,}) because "
        "archiving moves a database rather than copying it: the bridge is a "
        "staging area that empties as archiving proceeds. Both lists also "
        "include databases outside this reconciliation, which appear as "
        "Orphaned below."
    )

st.markdown(
    "<div class='rc-label rc-label-spaced'>Needs attention"
    "<span class='rc-hint'> &mdash; click to filter the results table</span></div>",
    unsafe_allow_html=True,
)
render_finding_buttons(findings, counts, len(result.findings), result.is_clean)

_explain = []
if counts.get(Outcome.ORPHANED):
    _explain.append(
        f"**Orphaned ({counts[Outcome.ORPHANED]:,})** &mdash; in the Data "
        "Bridge or Data Vault list, but on neither the on-prem inventory nor "
        "the scope list. Usually databases Amlin migrated themselves, or "
        "named differently on the two sides. Not migration work outstanding, "
        "but worth knowing the lists disagree."
    )
if counts.get(Outcome.MISSING):
    _explain.append(
        f"**Missing ({counts[Outcome.MISSING]:,})** &mdash; wanted, on prem, "
        "and in neither Data Bridge nor Data Vault: work not started. It will "
        "not equal *In Scope and Available* minus *Archived* minus *In "
        "transit*, because a database reported as Duplicate is counted there "
        "and not here."
    )
if _explain:
    st.caption("  \n".join(_explain))

for _conflict in config_conflicts():
    st.error(
        f"**Configuration conflict — {_conflict.fact}.** AMIGO's "
        f"`{_conflict.amigo_key}` is `{_conflict.amigo_value}` but "
        f"`{_conflict.reconcile_key}` is `{_conflict.reconcile_value}`. "
        "This tool is using the second. If the two are pointed at different "
        "tenants, the numbers below describe a different estate from the one "
        "AMIGO migrated."
    )

if result.targets_are_identical:
    st.error(
        "**The Data Bridge and Data Vault lists hold exactly the same names.** "
        "That almost always means the same file was loaded into both slots. "
        "Every imported database therefore reports as *Archived*, which is the "
        "most flattering possible answer and the least likely to be true — "
        "check the two uploads before reading anything below."
    )

elif not result.platform_checked:
    st.info(
        "**No Data Bridge list supplied**, so this is a Source-to-Data-Vault "
        "reconciliation. Databases not in Data Vault show as *Import "
        "unchecked* rather than *Missing* — the tool cannot say a database "
        "never imported without a Data Bridge list to check. Archived rows "
        "are still confirmed: being in the vault proves a database crossed."
    )

elif result.bridge_wholly_inside_vault:
    st.warning(
        f"**Every one of the {result.target_count} Data Bridge rows is also "
        "in Data Vault, so *In transit* is zero.** Archiving moves a database "
        "off the bridge, so the two lists should barely overlap — a bridge "
        "list wholly inside the vault usually means a Data Vault extract was "
        "loaded into step 3 as well. It is also what a finished migration "
        "looks like, so check which before reading the archiving numbers: "
        "nothing below is measuring Data Bridge."
    )

if not result.vault_checked:
    st.warning(
        "**No Data Vault list supplied**, so nothing here confirms archiving. "
        "Rows show as *Archiving unchecked*, which is not the same as done — "
        "archiving is a separate, explicit step that is not automatic on import."
    )

st.divider()

SIDE_TABS = (
    ("master_rows", "Source"),
    ("scope_rows", "Scope"),
    ("target_rows", "Data Bridge"),
    ("vault_rows", "Target"),
)
loaded_sides = [
    (key, title)
    for key, title in SIDE_TABS
    if key in st.session_state.raw_text or st.session_state.get(key)
]

tabs = st.tabs(
    [
        "Results",
        *(title for _, title in loaded_sides),
        *(
            ["Duplicate names"]
            if (
                st.session_state.master_rows
                or st.session_state.vault_rows
                or st.session_state.target_rows
            )
            else []
        ),
        "Report",
    ]
)
results_tab = tabs[0]
report_tab = tabs[-1]
vault_dupes_tab = (
    tabs[-2]
    if (
        st.session_state.master_rows
        or st.session_state.vault_rows
        or st.session_state.target_rows
    )
    else None
)

if vault_dupes_tab is not None:
    with vault_dupes_tab:
        side_rows = {
            "Source (On Prem)": st.session_state.master_rows or [],
            "Data Vault": st.session_state.vault_rows or [],
            "Data Bridge": st.session_state.target_rows or [],
        }
        side_groups = {
            side: duplicate_groups(rows) for side, rows in side_rows.items()
        }
        sides_with_dupes = [s for s, g in side_groups.items() if g]

        st.caption(
            "Names appearing more than once in any loaded list. Matching is "
            "by name, so a repeated name cannot be tied to a single row — "
            "every one of these is reported *Duplicate* and matches nothing. "
            "What it means depends on the list: the vault accepts repeated "
            "archives of one database by design, while the same name twice "
            "in the on-prem inventory is two different databases on two "
            "servers."
        )

        if not sides_with_dupes:
            keys = {
                name: (rows[0].data.get("_key_column", "?") if rows else "—")
                for name, rows in side_rows.items()
            }
            loaded = [
                f"{len(rows)} {name}" for name, rows in side_rows.items() if rows
            ]
            st.success(
                "No repeated names in " + ", ".join(loaded) + " rows."
                if loaded
                else "No lists loaded."
            )
            st.caption(
                "Compared on "
                + ", ".join(
                    f"**{keys[name]}** ({name})"
                    for name, rows in side_rows.items()
                    if rows
                )
                + ". A column that is unique by definition, such as "
                "`archiveId`, can never show a duplicate — if you expected "
                "some, check the match column on the file tab."
            )
            groups = []
        else:
            if len(sides_with_dupes) > 1:
                side = st.radio(
                    "Which list",
                    sides_with_dupes,
                    horizontal=True,
                    key="vault_dupe_side",
                    help=(
                        "More than one list holds repeated names. They are "
                        "separate problems: the vault repeats by design, the "
                        "Data Bridge list should not."
                    ),
                )
            else:
                side = sides_with_dupes[0]
            groups = side_groups[side]
            vault_rows_loaded = side_rows[side]

        if groups:
            affected = sum(len(group) for _, group in groups)
            st.warning(
                f"**{len(groups)}** names cover **{affected}** rows in the "
                f"**{side}** list. Each is reported *Duplicate* on the "
                "Results tab, and none of them matches."
            )

            summary: list[dict[str, object]] = []
            for _key, group in groups:
                ordered = newest_first(group)
                changes = compare_to_newest(group, ignore=("archiveId",))
                interesting = [
                    (field_name, new_value, old_value)
                    for field_name, new_value, old_value in changes
                    if field_name not in ARCHIVE_TIME_FIELDS
                ]
                servers = distinguishing_values(group, SERVER_FIELD) or (
                    distinguishing_values(group, "serverName")
                )
                summary.append(
                    {
                        "Name": ordered[0].name,
                        "Rows": len(group),
                        "Where": ", ".join(servers),
                        "Latest": _short_stamp(ordered[0]),
                        "Previous": _short_stamp(ordered[1])
                        if len(ordered) > 1
                        else "",
                        "What changed": (
                            ", ".join(
                                f"{field_name}: {old_value or '—'} → "
                                f"{new_value or '—'}"
                                for field_name, new_value, old_value in interesting
                            )
                            or "nothing but the date"
                        ),
                    }
                )

            st.dataframe(
                pd.DataFrame(summary),
                width="stretch",
                hide_index=True,
            )
            st.caption(
                "**What changed** compares the newest archive with the one "
                "before it, shown as *old → new*. "
                "*nothing but the date* means the two are identical apart "
                "from when they were taken — the same database archived "
                "twice, where the newest is the one that counts. Anything "
                "else, especially a change in `sizeInMb`, means the two "
                "archives hold different data under one name."
            )

            if len(groups) > 1:
                picked = st.selectbox(
                    "Inspect one name",
                    [group[0].name for _key, group in groups],
                    key="vault_dupe_pick",
                )
            else:
                picked = groups[0][1][0].name

            chosen = next(
                (group for _key, group in groups if group[0].name == picked),
                None,
            )
            if chosen:
                ordered = newest_first(chosen)
                st.markdown(f"**{html.escape(picked)}** — {len(ordered)} archives")
                detail = pd.DataFrame(
                    [
                        {
                            "Field": field_name,
                            "Previous": old_value or "—",
                            "Latest": new_value or "—",
                        }
                        for field_name, new_value, old_value in compare_to_newest(
                            chosen
                        )
                    ]
                )
                if detail.empty:
                    st.info(
                        "The two newest archives are identical on every "
                        "field, including their timestamps."
                    )
                else:
                    st.dataframe(
                        detail, width="stretch", hide_index=True
                    )
                if len(ordered) > 2:
                    st.caption(
                        f"Comparing the two newest of {len(ordered)}. The CSV "
                        "export carries every archive in every group."
                    )

            csv_rows = [
                {
                    "archiveName": row.name,
                    "key": key,
                    **{k: v for k, v in row.data.items()},
                }
                for key, group in groups
                for row in group
            ]
            slug = side.lower().split(" (")[0].replace(" ", "-")
            st.download_button(
                f"Download {side} duplicates (CSV)",
                data=pd.DataFrame(csv_rows).to_csv(index=False),
                file_name=f"{slug}-duplicates.csv",
                mime="text/csv",
            )

with report_tab:
    st.caption(
        "How far the estate has got. Each step counts the databases that "
        "reached it, so the drop between two steps is the work outstanding "
        "there."
    )

    report_result = result
    if result.scope_checked:
        basis = st.radio(
            "Measure against",
            ("The scope list", "Everything on prem"),
            horizontal=True,
            key="report_basis",
            help=(
                "The scope list answers whether we moved what we promised. "
                "Everything on prem answers whether anything is unaccounted "
                "for -- including databases nobody put on the scope list."
            ),
        )
        if basis == "Everything on prem":
            report_result = reconcile(
                st.session_state.master_rows,
                st.session_state.target_rows,
                st.session_state.vault_rows,
                None,
            )
            hidden = sum(
                1
                for row in result.rows
                if row.outcome is Outcome.OUT_OF_SCOPE
            )
            if hidden:
                st.info(
                    f"Treating all **{result.master_count}** on-prem "
                    f"databases as in scope. The **{hidden}** the scope list "
                    "excluded are now judged like any other — a database on "
                    "prem and absent from Data Bridge reads as **Missing**, "
                    "not *Not in scope*."
                )

    stages = funnel(report_result)
    widest = max((s.count for s in stages), default=0) or 1

    scope_base = next(
        (s.count for s in stages if s.name == "In scope"), 0
    )

    vault_reached = next(
        (s.count for s in stages if s.name == "Data Vault"), 0
    )
    bridge_total = next(
        (s.count for s in stages if s.name == "Data Bridge"), 0
    )
    bridge_transit = max(bridge_total - vault_reached, 0)

    scope_absent = next(
        (s.unreachable for s in stages if s.name == "In scope"), 0
    )

    rows_html = []
    previous_name = ""
    for stage in stages:
        pct = 100.0 * stage.count / widest
        share = (
            f"{100.0 * stage.count / scope_base:.0f}% of scope"
            if scope_base and stage.name not in ("On Prem", "In scope")
            else ""
        )
        lost = (
            f"−{stage.lost:,}"
            if stage.lost
            else ""
        )
        if stage.lost and stage.of_previous and previous_name:
            lost += (
                f"<span class='fn-kept'>{stage.percent_of_previous:.0f}% of "
                f"{html.escape(previous_name)}</span>"
            )
        done_seg_pct = 0.0
        done = stage.name == "Data Vault" and report_result.vault_checked
        bar_class = "fn-bar fn-bar-done" if done else "fn-bar"
        if done:
            done_seg_pct = pct

        bar_end_pct = pct

        if (
            stage.name == "Data Bridge"
            and report_result.vault_checked
            and stage.count
        ):
            archived_here = min(vault_reached, stage.count)
            transit_here = stage.count - archived_here
            done_pct = pct * archived_here / stage.count
            transit_pct = pct - done_pct
            done_class = "fn-seg fn-seg-done" + ("" if transit_here else " fn-seg-only")
            done_seg_pct = done_pct
            bar_html = (
                f"<div class='{done_class}' style='left:0;width:{done_pct:.1f}%'"
                f" title='{archived_here} archived in Data Vault'></div>"
            )
            if transit_here:
                bar_html += (
                    f"<div class='fn-seg fn-seg-transit' style='left:{done_pct:.1f}%;"
                    f"width:{transit_pct:.1f}%;border-radius:0 4px 4px 0'"
                    f" title='{transit_here} still in transit'></div>"
                )
        else:
            bar_html = f"<div class='{bar_class}' style='width:{pct:.1f}%'></div>"

        track_class = "fn-track"
        if stage.unreachable and widest:
            absent_pct = 100.0 * stage.unreachable / widest
            bar_end_pct = pct + absent_pct
            bar_html = (
                f"<div class='fn-seg fn-seg-scope' style='left:0;"
                f"width:{pct:.1f}%'"
                f" title='{stage.count} wanted and on prem'></div>"
                f"<div class='fn-seg fn-seg-absent' style='left:{pct:.1f}%;"
                f"width:{absent_pct:.1f}%'"
                f" title='{stage.unreachable} wanted but not on prem'></div>"
            )

        gap_html = ""
        room = 100.0 - bar_end_pct
        if stage.lost and room > 20.0:
            kept = (
                f"<span class='fn-gap-kept'>not yet in {html.escape(stage.name)}</span>"
                if stage.of_previous
                else ""
            )
            gap_html = (
                f"<div class='fn-gap' style='left:{bar_end_pct:.1f}%'>"
                f"{MINUS}{stage.lost:,}{kept}</div>"
            )
            lost = ""

        value_inside = done_seg_pct >= 9.0
        if value_inside:
            value_html = (
                f"<div class='fn-value-in'>{stage.count:,}</div>"
                f"<div class='fn-value'><span class='fn-share'>{share}</span></div>"
            )
        else:
            value_html = (
                f"<div class='fn-value'>{stage.count:,}"
                f"<span class='fn-share'>{share}</span></div>"
            )

        rows_html.append(
            "<div class='fn-row'>"
            f"<div class='fn-stage'>{html.escape(stage.name)}</div>"
            f"<div class='{track_class}'>"
            f"{bar_html}"
            f"{gap_html}"
            f"{value_html}"
            "</div>"
            f"<div class='fn-drop'>{lost}</div>"
            f"<div class='fn-note'>{html.escape(stage.note)}</div>"
            "</div>"
        )
        previous_name = stage.name
    st.markdown(
        f"<div class='fn'>{''.join(rows_html)}</div>", unsafe_allow_html=True
    )

    st.caption(
        "Each row shows two figures: the share **of scope** (the same base "
        "down the column) and the share **of the stage above** (a different "
        "base on every row). The red number is what has not reached that "
        "stage yet -- databases waiting or in transit, not databases lost."
    )

    keys = []
    if report_result.vault_checked and bridge_transit:
        keys.append(
            "<span><span class='fn-key' style='background:#2f6f4f'></span>"
            f"Archived in Data Vault ({vault_reached:,})</span>"
            "<span><span class='fn-key' style='background:#d98324'></span>"
            f"In transit on the bridge ({bridge_transit:,})</span>"
        )
    if scope_absent:
        keys.append(
            "<span><span class='fn-key' style='background:#3a6ea5'></span>"
            f"Wanted and on prem ({scope_base:,})</span>"
            "<span><span class='fn-key' style='background:"
            "repeating-linear-gradient(135deg,#c4342c,#c4342c 3px,"
            "#9b241d 3px,#9b241d 6px)'></span>"
            f"Wanted but not on prem ({scope_absent:,})</span>"
        )
    if keys:
        st.markdown(
            f"<div class='fn-legend'>{''.join(keys)}</div>",
            unsafe_allow_html=True,
        )

    if scope_absent:
        st.caption(
            f"**The percentages above are of the {scope_base:,} databases that "
            f"exist on prem and were wanted.** The {scope_absent:,} on the "
            "scope list with no on-prem database are the hatched red tail of "
            "the In scope bar, and are left out of the base: nothing can flow "
            "from a database that "
            "is not there, and counting them in would mean progress could "
            "never read 100%. They are a data-quality question about the scope "
            "list, not migration work outstanding."
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
        st.dataframe(funnel_frame, hide_index=True, width="stretch")

    if report_result.vault_checked:
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

    unscoped = report_result is not result
    st.download_button(
        "Download this report (CSV)",
        data=funnel_to_csv(report_result),
        file_name=(
            "reconciliation-report-whole-estate.csv"
            if unscoped
            else "reconciliation-report.csv"
        ),
        mime="text/csv",
        help=(
            "Measured against every on-prem database."
            if unscoped
            else "Measured against the scope list."
        ),
    )

    st.divider()
    st.subheader("Data Vault activity")

    vault_activity = rows_to_frame(st.session_state.get("vault_rows"))
    _size_col = None
    if len(vault_activity):
        _size_col = next(
            (
                c
                for c in vault_activity.columns
                if c.strip().casefold() in {f.casefold() for f in SIZE_FIELDS_MB}
            ),
            None,
        )

    if not len(vault_activity):
        st.caption(
            "Load the Data Vault archive list to see activity. Nothing here "
            "needs a reconciliation run -- it reads the archive rows directly."
        )
    else:
        created_col = _first_column(vault_activity, CREATED_DATE_COLUMNS)
        creator_col = _first_column(vault_activity, CREATOR_COLUMNS)

        if created_col is None:
            st.caption(
                "These rows carry no creation date, so activity over time "
                "cannot be derived from them."
            )
        else:
            activity = vault_activity.copy()
            stamps = _parse_stamps(activity[created_col])
            unparsed = int(stamps.isna().sum())
            activity = activity[stamps.notna()].copy()
            activity["_day"] = (
                stamps[stamps.notna()].dt.tz_localize(None).dt.floor("D")
            )

            if unparsed:
                st.caption(
                    f"{unparsed} row(s) have no usable creation date and are "
                    "excluded from the counts below."
                )

            if _size_col is not None:
                activity["_mb"] = pd.to_numeric(
                    activity[_size_col], errors="coerce"
                ).fillna(0)

            if creator_col is not None:
                automated_mask = (
                    activity[creator_col]
                    .astype(str)
                    .str.strip()
                    .str.casefold()
                    .isin(AUTOMATED_ACTORS)
                )
            else:
                automated_mask = pd.Series(False, index=activity.index)
            activity["_source"] = ["Automation" if a else "Manual" for a in automated_mask]

            manual = activity[~automated_mask]
            automated = activity[automated_mask]

            def _rate(frame: pd.DataFrame) -> tuple[int, int, float, float]:
                if not len(frame):
                    return 0, 0, 0.0, 0.0
                by_day = frame.groupby("_day").size()
                mean = float(by_day.mean())
                recent = float(by_day.tail(3).mean()) if len(by_day) >= 3 else mean
                return int(by_day.sum()), len(by_day), mean, recent

            m_total, m_days, m_mean, m_recent = _rate(manual)
            a_total, a_days, a_mean, a_recent = _rate(automated)

            left_m, mid_m, right_m = st.columns(3)
            left_m.metric("Manual", f"{m_total:,}")
            left_m.caption(
                f"{m_mean:,.0f}/day over {m_days} active day(s)"
                if m_days
                else "no archives"
            )
            mid_m.metric("Automation", f"{a_total:,}")
            mid_m.caption(
                f"{a_mean:,.0f}/day over {a_days} active day(s)"
                if a_days
                else "none yet"
            )
            right_m.metric("Total in Vault", f"{m_total + a_total:,}")
            right_m.caption(
                f"{100.0 * a_total / (m_total + a_total):.1f}% automated"
                if (m_total + a_total)
                else ""
            )

            chart_window = st.radio(
                "Chart range",
                ["Last 30 days", "Last 90 days", "All"],
                index=0,
                horizontal=True,
                key="vault_activity_window",
            )
            window_days = {"Last 30 days": 30, "Last 90 days": 90}.get(chart_window)
            since = (
                pd.Timestamp.now(tz="UTC").tz_localize(None).floor("D")
                - pd.Timedelta(days=window_days - 1)
                if window_days
                else None
            )

            def _windowed(frame: pd.DataFrame) -> pd.DataFrame:
                if since is None:
                    return frame
                days = pd.date_range(since, since + pd.Timedelta(days=window_days - 1))
                return frame.reindex(days, fill_value=0)

            st.markdown("**DB Archived per day**")
            per_day_split = (
                activity.groupby(["_day", "_source"]).size().unstack(fill_value=0)
            )
            for column in ("Manual", "Automation"):
                if column not in per_day_split.columns:
                    per_day_split[column] = 0
            per_day_split = per_day_split[["Manual", "Automation"]]
            st.bar_chart(_windowed(per_day_split), height=400)

            if _size_col is not None:
                st.markdown("**GB per day**")
                gb_split = (
                    activity.groupby(["_day", "_source"])["_mb"].sum().unstack(fill_value=0)
                    / 1024
                ).round(2)
                for column in ("Manual", "Automation"):
                    if column not in gb_split.columns:
                        gb_split[column] = 0.0
                st.bar_chart(_windowed(gb_split[["Manual", "Automation"]]), height=400)

            table = per_day_split.copy()
            table["Total"] = table["Manual"] + table["Automation"]
            if _size_col is not None:
                table["GB"] = (
                    activity.groupby("_day")["_mb"].sum() / 1024
                ).round(2)
            table = table.sort_index(ascending=False)
            st.dataframe(table.reset_index().rename(columns={"_day": "day"}),
                         hide_index=True, width="stretch")

            if creator_col is not None:
                agg = {"archives": (creator_col, "size")}
                if _size_col is not None:
                    agg["GB"] = ("_mb", lambda s: round(s.sum() / 1024, 2))

                st.markdown("**Per person** (manual only)")
                if len(manual):
                    st.dataframe(
                        manual.groupby(creator_col)
                        .agg(**agg)
                        .reset_index()
                        .sort_values("archives", ascending=False),
                        hide_index=True,
                        width="stretch",
                    )
                else:
                    st.caption("No manual archives in this listing.")

                if len(automated):
                    st.markdown("**Automation**")
                    st.dataframe(
                        automated.groupby(creator_col)
                        .agg(**agg)
                        .reset_index()
                        .sort_values("archives", ascending=False),
                        hide_index=True,
                        width="stretch",
                    )

            st.markdown("**Manual rate, and what it implies**")
            if not m_days:
                st.caption("No manual archives in this listing.")
            else:
                projection = (
                    f"about {8000 / m_recent:,.0f} working day(s)"
                    if m_recent
                    else "an unknown number of days"
                )
                st.caption(
                    f"{m_total:,} archive(s) by hand over {m_days} active "
                    f"day(s) -- mean {m_mean:,.0f}/day, {m_recent:,.0f}/day "
                    f"over the last three. At the recent rate, 8,000 databases "
                    f"would take {projection}. This is the figure an automated "
                    "rate has to beat; record it alongside any POC timing "
                    "(TASK-0022). It counts archives rather than volume, so a "
                    "day of small databases and a day of large ones weigh the "
                    "same -- read it with the GB chart above, not instead of it."
                )
                if a_days:
                    st.caption(
                        f"Automation: {a_total:,} archive(s) over {a_days} "
                        f"active day(s), mean {a_mean:,.0f}/day. **Too small a "
                        "sample to extrapolate from** -- it is early POC "
                        "traffic, not a sustained rate."
                    )

            st.download_button(
                "Download activity by day (CSV)",
                data=table.reset_index().rename(columns={"_day": "day"}).to_csv(index=False),
                file_name="vault-activity-by-day.csv",
                mime="text/csv",
                width="stretch",
            )

    if len(vault_activity) and _size_col is not None:
        st.divider()
        st.subheader("Data Vault size distribution")

        sizes_mb = pd.to_numeric(vault_activity[_size_col], errors="coerce").dropna()
        if not len(sizes_mb):
            st.caption(f"No usable numbers in {_size_col!r}.")
        else:
            bands = (
                ("< 1 GB", 0, 1024),
                ("1-10 GB", 1024, 10240),
                ("10-50 GB", 10240, 51200),
                ("50-90 GB", 51200, 92160),
                ("90-200 GB", 92160, 204800),
                ("200-500 GB", 204800, 512000),
                (">= 500 GB", 512000, None),
            )
            rows_out = []
            for label, lo, hi in bands:
                sel = sizes_mb[(sizes_mb >= lo) & ((sizes_mb < hi) if hi else True)]
                rows_out.append(
                    {
                        "band": label,
                        "archives": len(sel),
                        "GB": round(float(sel.sum()) / 1024, 2),
                    }
                )
            dist = pd.DataFrame(rows_out)

            total_mb = float(sizes_mb.sum())
            heavy = sizes_mb[sizes_mb >= 92160]
            left_d, mid_d, right_d = st.columns(3)
            left_d.metric("Total", f"{total_mb / 1024:,.0f} GB")
            left_d.caption(f"{total_mb / 1048576:,.2f} TB")
            mid_d.metric("Largest", f"{sizes_mb.max() / 1024:,.1f} GB")
            right_d.metric(
                "90 GB and over",
                f"{len(heavy):,}",
                f"{100 * float(heavy.sum()) / total_mb:.0f}% of volume"
                if total_mb
                else None,
            )

            st.bar_chart(dist.set_index("band")["archives"], height=240)
            st.dataframe(dist, hide_index=True, width="stretch")
            st.caption(
                "Bands match the source-side analysis (RSK-0006), so the Vault "
                "contents compare against the estate they came from. Sizes read "
                f"from {_size_col!r}."
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
            width="stretch",
        )

        run_ids = [r.id for r in history]
        reopen_col, earlier_col, later_col = st.columns(3)
        with reopen_col:
            chosen_run = st.selectbox(
                "Reopen a run",
                run_ids,
                index=run_ids.index(st.session_state.run_id)
                if st.session_state.run_id in run_ids
                else 0,
                key="reopen_run",
            )
            if st.button("Reopen", width="stretch"):
                st.session_state.result = store.load_run(int(chosen_run))
                st.session_state.run_id = int(chosen_run)
                st.session_state.outcome_filter = None
                st.rerun()
        with earlier_col:
            earlier = st.selectbox(
                "Compare -- earlier run",
                run_ids,
                index=min(1, len(run_ids) - 1),
                key="cmp_earlier",
            )
        with later_col:
            later = st.selectbox("Compare -- later run", run_ids, index=0, key="cmp_later")
            compare = st.button("Compare", width="stretch")

        if compare:
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

    st.dataframe(view, hide_index=True, width="stretch")
    st.caption(f"Showing {len(view)} of {len(frame)} rows.")

    left_dl, right_dl = st.columns(2)
    with left_dl:
        st.download_button(
            f"Download these {len(view)} rows (CSV)",
            data=view.to_csv(index=False),
            file_name="reconciliation-filtered.csv",
            mime="text/csv",
            width="stretch",
            disabled=len(view) == len(frame),
            help="What the table is showing, with the filter applied.",
        )
    with right_dl:
        st.download_button(
            f"Download all {len(frame)} rows (CSV)",
            data=to_csv(result),
            file_name="reconciliation.csv",
            mime="text/csv",
            width="stretch",
            help="Every row, whatever the filter says.",
        )

file_tabs = tabs[1 : 1 + len(loaded_sides)]
for tab, (key, title) in zip(file_tabs, loaded_sides, strict=True):
    with tab:
        if key in st.session_state.raw_text:
            filename, text = st.session_state.raw_text[key]
            render_file_tab(title, filename, text, key)
        else:
            label = st.session_state.get(f"{key.removesuffix('_rows')}_label")
            render_file_tab(
                title,
                label or title,
                None,
                key,
                fetched=st.session_state.get(key) or [],
            )
