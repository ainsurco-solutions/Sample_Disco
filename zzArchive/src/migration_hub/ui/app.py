from __future__ import annotations

import getpass
import html
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from pandas.io.formats.style import Styler
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from migration_hub.config.settings import Settings
from migration_hub.core.models import ApiTransaction, BatchRun, MigrationEvent, MigrationFile
from migration_hub.core.registry import Registry
from migration_hub.core.states import (
    SETTLED_STATES,
    BatchDestination,
    BatchState,
    FileState,
    done_states,
)
from migration_hub.observability import audit, controls, metrics
from migration_hub.orchestration import batches as batch_ops
from migration_hub.orchestration import queue_check
from migration_hub.orchestration.batch_identity import derive_batch_id
from migration_hub.producers import scanner, validator

load_dotenv()

_BadgeColor = Literal[
    "red", "orange", "yellow", "blue", "green", "violet", "gray", "grey", "primary"
]

_AddBatchWizard = dict[str, object]

_STATE_PROGRESS = {
    FileState.DISCOVERED: 10,
    FileState.VALIDATED: 25,
    FileState.REJECTED: 0,
    FileState.UPLOADING: 50,
    FileState.UPLOADED: 60,
    FileState.IMPORTING: 70,
    FileState.VERIFYING: 80,
    FileState.BRIDGED: 85,
    FileState.ARCHIVING: 92,
    FileState.ARCHIVE_FAILED: 85,
    FileState.COMPLETED: 100,
    FileState.FAILED: 45,
    FileState.ABANDONED: 0,
}

_BATCH_STATE_COLOR: dict[BatchState, _BadgeColor] = {
    BatchState.PLANNED: "gray",
    BatchState.RUNNING: "blue",
    BatchState.PAUSED: "orange",
    BatchState.DONE: "green",
}

def _batch_display_state(
    state: BatchState,
    counts: dict[FileState, int],
    destination: BatchDestination = BatchDestination.VAULT,
) -> tuple[str, _BadgeColor]:
    total = sum(counts.values())
    outstanding = total - sum(counts[s] for s in done_states(destination))
    if state in (BatchState.PLANNED, BatchState.RUNNING) and total > 0 and outstanding == 0:
        failed = (
            counts[FileState.FAILED]
            + counts[FileState.ABANDONED]
            + counts[FileState.ARCHIVE_FAILED]
        )
        if destination is BatchDestination.BRIDGE:
            return (
                ("On Data Bridge, with issues", "orange") if failed else ("On Data Bridge", "green")
            )
        return ("Completed with issues", "orange") if failed else ("Completed", "green")
    return state.value.title(), _BATCH_STATE_COLOR[state]

_DESTINATION_LABEL = {
    BatchDestination.VAULT: "Data Vault",
    BatchDestination.BRIDGE: "Data Bridge only",
}

def _outstanding(counts: dict[FileState, int], destination: BatchDestination) -> int:
    return sum(counts.values()) - sum(counts[s] for s in done_states(destination))

_TRACKER_PALETTE = {
    "light": {
        "surface": "#FFFFFF",
        "line_strong": "#D8D9DB",
        "ink": "#1a1a1a",
        "faint": "#6B6C70",
        "ok": "#2E7D5B",
        "accent": "#1B1464",
        "accent_wash": "#E8E7F2",
    },
    "dark": {
        "surface": "#11161C",
        "line_strong": "#313D46",
        "ink": "#E7ECEF",
        "faint": "#5D6C77",
        "ok": "#57B98A",
        "accent": "#00E6F0",
        "accent_wash": "#0E2A2D",
    },
}

@st.cache_resource
def _engine(database_url: str) -> Engine:
    return create_engine(database_url)

def _selected_rows(dataframe_event: object) -> list[int]:
    return dataframe_event.selection.rows

def _load_settings() -> Settings:
    environment = os.environ.get("MIGRATION_HUB_ENV", "dev")
    return Settings.load(environment=environment, config_dir=Path("config"))

def render() -> None:
    st.set_page_config(page_title="Migration Hub", page_icon="🧭", layout="wide")

    settings = _load_settings()
    registry = Registry(_engine(settings.database_url))

    if "selected_batch" not in st.session_state:
        existing = registry.list_batches()
        st.session_state["selected_batch"] = existing[0].batch_id if existing else None

    title_col, action_col = st.columns([5, 1], vertical_alignment="bottom")
    with title_col:
        st.title("Migration Hub")
    with action_col:
        if st.button(
            "Start migration", type="primary", icon=":material/rocket_launch:", width="stretch"
        ):
            st.session_state["add_batch_open"] = True
            st.session_state["add_batch_wizard"] = {"step": "select"}

    with st.container(horizontal=True, vertical_alignment="center"):
        env_color: _BadgeColor = "red" if settings.environment == "prod" else "orange"
        st.badge(
            settings.environment.upper(),
            color=env_color,
            icon=":material/dns:",
            help="Actions in this environment affect its live registry and services.",
        )
        if settings.dry_run:
            st.badge("Dry run", color="gray", icon=":material/science:")
        if st.button("Refresh now", icon=":material/refresh:"):
            st.rerun()
    if st.session_state.get("add_batch_open"):
        _add_batch_dialog(registry, settings)

    _render_overview(registry, settings)
    st.space("medium")

    selected = st.session_state.get("selected_batch")
    if not selected:
        st.info("No batches yet. Click **Start migration** above to start one.")
        return

    st.subheader(f"Batch {selected}")
    _live_batch_action_bar(registry, settings, selected)
    _render_live_log(registry, selected)
    st.space("small")
    _live_batch_tabs(registry, settings, selected)

_BATCH_REFRESH = "8s"

@st.fragment(run_every=_BATCH_REFRESH)
def _live_batch_action_bar(registry: Registry, settings: Settings, batch: str) -> None:
    _render_launch_notes(batch)
    _render_batch_action_bar(registry, settings, batch)

@st.fragment(run_every=_BATCH_REFRESH)
def _live_batch_tabs(registry: Registry, settings: Settings, batch: str) -> None:
    _render_tabs(registry, settings, batch)

@st.fragment(run_every="8s")
def _render_overview(registry: Registry, settings: Settings) -> None:
    _render_unknown_states(registry)
    _kpi_strip_global(registry)
    st.space("small")
    _render_queue_panel(settings)
    st.space("small")
    _render_operational_observability(registry, batch_id=None)
    st.space("small")
    _render_batch_log(registry)

_RECOMMENDATION_COLOR: dict[queue_check.Recommendation, _BadgeColor] = {
    queue_check.Recommendation.PUSH: "green",
    queue_check.Recommendation.HOLD: "orange",
    queue_check.Recommendation.INVESTIGATE: "red",
    queue_check.Recommendation.UNKNOWN: "gray",
}

_QUEUE_STALE_TTLS = 3

_QUEUE_LAUNCH_GRACE_SECONDS = 30

def _queue_view(
    snapshot: queue_check.Snapshot | None, *, now: datetime
) -> tuple[str, list[tuple[str, queue_check.Recommendation, str]]]:
    if snapshot is None:
        return "Not checked yet", []
    age_minutes = max(0, int((now - snapshot.written_at).total_seconds() // 60))
    stale = age_minutes >= snapshot.ttl_minutes * _QUEUE_STALE_TTLS
    headline = f"Read {_ago(age_minutes)}" + (" -- stale, check again" if stale else "")
    rows = []
    for q in snapshot.instances:
        recommendation = queue_check.Recommendation.UNKNOWN if stale else q.recommendation
        if q.read_ok:
            oldest = (
                f"oldest {q.oldest_active_minutes} min"
                if q.oldest_active_minutes is not None
                else "none active"
            )
            detail = (
                f"{sum(q.queued.values())} queued, {sum(q.running.values())} running "
                f"({q.active_imports} imports, {q.ours_active} ours) -- {oldest}; "
                f"{q.failed_last_24h} failed in 24 h"
            )
        else:
            detail = q.error or "the job list could not be read"
        rows.append((q.instance, recommendation, detail))
    return headline, rows

def _render_queue_panel(settings: Settings) -> None:
    now = datetime.now(UTC)
    snapshot = queue_check.read_snapshot(settings.queue_snapshot_path)
    headline, rows = _queue_view(snapshot, now=now)

    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader("Data Bridge queue")
            st.caption(headline)
            launched = st.session_state.get("queue_check_launched_at")
            recently = (
                launched is not None
                and (now - launched).total_seconds() < _QUEUE_LAUNCH_GRACE_SECONDS
            )
            fresh = snapshot is not None and now < snapshot.fresh_until()
            if settings.dry_run:
                reason = "dry_run is on -- no vendor calls"
            elif fresh and snapshot is not None:
                reason = f"next read allowed from {snapshot.fresh_until():%H:%M:%S} UTC"
            elif recently:
                reason = "a check is already running"
            else:
                reason = "read each instance's job list now"
            if st.button(
                "Check now",
                icon=":material/sync:",
                key="queue_check_now",
                disabled=settings.dry_run or fresh or recently,
                help=reason,
            ):
                handle = batch_ops.start_queue_subprocess(environment=settings.environment)
                st.session_state["queue_check_launched_at"] = now
                st.caption(f"Started (pid `{handle.pid}`); output in `{handle.log_path}`.")
        if not rows:
            st.caption(
                "Press **Check now** before starting a batch: it shows whether Data "
                "Bridge is already busy."
            )
        for instance, recommendation, detail in rows:
            with st.container(horizontal=True, vertical_alignment="center"):
                st.badge(str(recommendation), color=_RECOMMENDATION_COLOR[recommendation])
                st.write(f"**{instance}**")
                st.caption(detail)

def _render_unknown_states(registry: Registry) -> None:
    unknown = registry.unknown_states()
    if not unknown:
        return
    listed = ", ".join(f"`{s}` ({n})" for s, n in sorted(unknown.items()))
    st.warning(
        f"**This registry holds file states this build does not know: "
        f"{listed}.** They are left out of the counts below, so those will "
        "not sum to the total number of files. A state retired by a code "
        "change leaves its rows behind; run `python -m alembic upgrade head` "
        "to move them."
    )

def _kpi_strip_global(registry: Registry) -> None:
    counts = registry.counts_by_state(batch_id=None)
    in_flight = (
        counts[FileState.UPLOADING]
        + counts[FileState.IMPORTING]
        + counts[FileState.VERIFYING]
        + counts[FileState.BRIDGED]
        + counts[FileState.ARCHIVING]
    )
    issues = (
        counts[FileState.FAILED] + counts[FileState.ABANDONED] + counts[FileState.ARCHIVE_FAILED]
    )
    pending_archives = len(registry.pending_archives(batch_id=None))
    completed_recently = registry.count_transitions_since(
        batch_id=None,
        to_state=FileState.COMPLETED,
        since=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
    )

    tiles = [
        ("gray", "Discovered", counts[FileState.DISCOVERED], "awaiting validation"),
        ("blue", "In flight", in_flight, "uploading · importing · archiving"),
        (
            "green",
            "Completed",
            counts[FileState.COMPLETED],
            f"+{completed_recently} in the last hour"
            if completed_recently
            else "none in the last hour",
        ),
        (
            "orange",
            "Pending archive",
            pending_archives,
            "on Data Bridge, not in Vault" if pending_archives else "none",
        ),
        (
            "red",
            "Failed / abandoned",
            issues,
            "needs attention" if issues else "none",
        ),
    ]
    st.markdown(
        """
        <style>
          .mh-summary-row { display:flex; flex-wrap:wrap; gap:.6rem; margin:.15rem 0 .25rem; }
          .mh-summary-card {
            flex:1 1 150px; min-width:150px; padding:.5rem .7rem;
            border:1px solid rgba(128,128,128,.28); border-radius:8px;
            background:rgba(128,128,128,.06);
          }
          .mh-summary-value {
            font-size:1.5rem; font-weight:700; line-height:1.05;
            font-variant-numeric:tabular-nums;
          }
          .mh-summary-title { font-size:.88rem; font-weight:600; margin-top:.1rem; }
          .mh-summary-note { font-size:.74rem; opacity:.7; margin-top:.15rem; }
          .mh-summary-card[data-tone="green"] .mh-summary-value { color:#2E7D5B; }
          .mh-summary-card[data-tone="orange"] .mh-summary-value { color:#A6690E; }
          .mh-summary-card[data-tone="red"] .mh-summary-value { color:#B23A3A; }
          .mh-summary-card[data-tone="blue"] .mh-summary-value { color:#1B1464; }
          .mh-summary-card[data-empty="true"] { opacity:.6; }
          .mh-summary-row[data-theme="dark"] [data-tone="green"] .mh-summary-value {
            color:#57B98A;
          }
          .mh-summary-row[data-theme="dark"] [data-tone="orange"] .mh-summary-value {
            color:#D9A441;
          }
          .mh-summary-row[data-theme="dark"] [data-tone="red"] .mh-summary-value {
            color:#E2726B;
          }
          .mh-summary-row[data-theme="dark"] [data-tone="blue"] .mh-summary-value {
            color:#00E6F0;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )
    cards = "".join(
        f'<div class="mh-summary-card" data-tone="{swatch}" data-empty="{str(value == 0).lower()}">'
        f'<div class="mh-summary-value">{value}</div>'
        f'<div class="mh-summary-title">{html.escape(label)}</div>'
        f'<div class="mh-summary-note">{html.escape(sub)}</div></div>'
        for swatch, label, value, sub in tiles
    )
    theme = "dark" if st.context.theme.type == "dark" else "light"
    st.markdown(
        f'<div class="mh-summary-row" data-theme="{theme}">{cards}</div>',
        unsafe_allow_html=True,
    )

def _render_operational_observability(registry: Registry, *, batch_id: str | None) -> None:
    left, right = st.columns(2)
    with left:
        _render_throughput_panel(registry, batch_id=batch_id)
        if batch_id is None:
            _render_needs_action_queue(registry)
    with right:
        _render_api_call_panel(registry, batch_id=batch_id)

def _render_throughput_panel(registry: Registry, *, batch_id: str | None) -> None:
    snapshot = metrics.throughput(registry=registry, batch_id=batch_id, window_hours=24)
    file_metrics = registry.file_metrics(batch_id=batch_id)
    remaining_bytes = max(0, file_metrics["total_bytes"] - file_metrics["completed_bytes"])
    eta = metrics.projected_completion(remaining_bytes=remaining_bytes, snapshot=snapshot)
    failure_rate = metrics.failure_rate(registry=registry, batch_id=batch_id)

    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader("Throughput / ETA")
            st.badge("Registry measured", color="green", icon=":material/speed:")
        cols = st.columns(4)
        with cols[0]:
            st.metric("Speed", f"{_format_bytes(snapshot.bytes_per_second)}/s")
            st.caption(f"{snapshot.files_completed} completed in 24h")
        with cols[1]:
            st.metric("Completed", _format_bytes(file_metrics["completed_bytes"]))
            st.caption(f"{file_metrics['total_count']} file(s) in scope")
        with cols[2]:
            st.metric("Remaining", _format_bytes(remaining_bytes))
            st.caption("by registered file size")
        with cols[3]:
            eta_text = _format_timedelta(eta) if eta is not None else "Pending"
            st.metric("ETA", eta_text)
            if eta is None:
                st.caption(
                    f"needs {metrics.MIN_COMPLETIONS_FOR_ETA}+ completions in 24h -- "
                    f"failure rate {failure_rate:.1%}"
                )
            else:
                st.caption(f"projection, not a commitment -- failure rate {failure_rate:.1%}")

def _render_api_call_panel(registry: Registry, *, batch_id: str | None) -> None:
    summary = metrics.api_call_summary(registry=registry, batch_id=batch_id, window_minutes=15)
    risk_color: dict[str, _BadgeColor] = {
        "normal": "green",
        "watch": "orange",
        "investigate": "red",
    }

    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader("API calls")
            st.badge(
                f"Runaway risk: {summary.risk}",
                color=risk_color[summary.risk],
                icon=":material/network_check:",
            )
        cols = st.columns(4)
        with cols[0]:
            st.metric("Calls / min", f"{summary.calls_per_minute:.1f}")
            st.caption(f"{summary.calls} calls in 15m")
        with cols[1]:
            st.metric("Errors", summary.errors)
            st.caption(f"{summary.error_rate:.1%} error rate")
        with cols[2]:
            st.metric("Slow calls", summary.slow_calls)
            st.caption(f">= {summary.slow_threshold_ms // 1000}s")
        with cols[3]:
            top_endpoint = summary.top_endpoints[0] if summary.top_endpoints else None
            st.metric("Top endpoint", top_endpoint.calls if top_endpoint else 0)
            st.caption(top_endpoint.endpoint if top_endpoint else "none")

        if summary.risk == "investigate":
            st.error("Call pattern is abnormal. Pause new work and inspect the Vendor calls tab.")
        elif summary.risk == "watch":
            st.warning("Call pattern is elevated. Watch for repeated 4xx/5xx responses.")

        _render_api_summary_tables(summary)

def _render_api_summary_tables(summary: metrics.ApiCallSummary) -> None:
    if not summary.top_endpoints and not summary.status_families:
        st.caption("No API calls logged in the last 15 minutes.")
        return

    endpoint_rows = [
        {"endpoint": item.endpoint, "calls": item.calls, "errors": item.errors}
        for item in summary.top_endpoints
    ]
    status_rows = [
        {"status": family, "calls": count}
        for family, count in sorted(summary.status_families.items())
    ]
    left, right = st.columns(2)
    with left:
        st.dataframe(
            endpoint_rows,
            column_config={
                "endpoint": st.column_config.TextColumn("Endpoint"),
                "calls": st.column_config.NumberColumn("Calls"),
                "errors": st.column_config.NumberColumn("Errors"),
            },
            hide_index=True,
            height=180,
        )
    with right:
        st.dataframe(
            status_rows,
            column_config={
                "status": st.column_config.TextColumn("Status"),
                "calls": st.column_config.NumberColumn("Calls"),
            },
            hide_index=True,
            height=180,
        )

def _render_needs_action_queue(registry: Registry) -> None:
    rows: list[dict[str, str | int]] = []
    for batch in registry.list_batches():
        if batch.state == BatchState.DONE:
            continue
        counts = registry.counts_by_state(batch_id=batch.batch_id)
        pending_archive = len(registry.pending_archives(batch_id=batch.batch_id))
        failed = (
            counts[FileState.FAILED]
            + counts[FileState.ABANDONED]
            + counts[FileState.ARCHIVE_FAILED]
        )
        destination = BatchDestination(batch.destination)
        outstanding = _outstanding(counts, destination)

        if pending_archive and destination is BatchDestination.VAULT:
            rows.append(
                {
                    "priority": 1,
                    "batch": batch.batch_id,
                    "finding": f"{pending_archive} file(s) pending archive",
                    "next_action": "Archive into Data Vault",
                }
            )
        elif pending_archive:
            rows.append(
                {
                    "priority": 4,
                    "batch": batch.batch_id,
                    "finding": f"{pending_archive} file(s) on Data Bridge (its destination)",
                    "next_action": "Archive into Data Vault when ready",
                }
            )
        if failed:
            rows.append(
                {
                    "priority": 2,
                    "batch": batch.batch_id,
                    "finding": f"{failed} failed/abandoned/archive issue(s)",
                    "next_action": "Review and retry",
                }
            )
        if outstanding:
            rows.append(
                {
                    "priority": 3,
                    "batch": batch.batch_id,
                    "finding": f"{outstanding} file(s) still in flight",
                    "next_action": "Monitor progress",
                }
            )

    rows = sorted(rows, key=lambda r: (int(r["priority"]), str(r["batch"])))
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader("Needs action")
            st.badge(str(len(rows)), color="orange" if rows else "green")
        if not rows:
            st.caption("No batches need operator action right now.")
        for i, row in enumerate(rows):
            if i == 3:
                with st.expander(f"Show {len(rows) - 3} more"):
                    _render_needs_action_rows(rows[3:], offset=3)
                break
            _render_needs_action_rows([row], offset=i)

def _render_needs_action_rows(rows: list[dict[str, str | int]], *, offset: int) -> None:
    for i, row in enumerate(rows, start=offset):
        details, action = st.columns([4, 1], vertical_alignment="center")
        with details:
            color: _BadgeColor = "orange" if int(row["priority"]) <= 2 else "blue"
            st.badge(str(row["finding"]), color=color)
            st.caption(f"{row['batch']} · {row['next_action']}")
        with action:
            if st.button(
                "Open",
                key=f"needs_action_open_{i}_{row['batch']}",
                icon=":material/open_in_new:",
            ):
                _open_batch(str(row["batch"]))

def _open_batch(batch: str) -> None:
    if st.session_state.get("selected_batch") == batch:
        st.toast(
            f"**{batch}** is already open -- scroll down to **Batch {batch}**.",
            icon=":material/arrow_downward:",
        )
        return
    st.session_state["selected_batch"] = batch
    st.rerun()

def _render_batch_log(registry: Registry) -> None:
    batches = registry.list_batches()
    if not batches:
        st.caption("No batches yet -- click **Start migration** to start one.")
        return

    rows = []
    attention_batches: set[str] = set()
    archive_batches = {file.batch_id for file in registry.pending_archives(batch_id=None)}
    for b in batches:
        counts = registry.counts_by_state(batch_id=b.batch_id)
        label, _color = _batch_display_state(
            BatchState(b.state), counts, BatchDestination(b.destination)
        )

        total = sum(counts.values())
        is_done = total > 0 and _outstanding(counts, BatchDestination(b.destination)) == 0
        failed = (
            counts[FileState.FAILED]
            + counts[FileState.ABANDONED]
            + counts[FileState.ARCHIVE_FAILED]
        )
        if b.state != BatchState.DONE and (
            failed
            or counts[FileState.REJECTED]
            or (b.destination == BatchDestination.VAULT and b.batch_id in archive_batches)
        ):
            attention_batches.add(b.batch_id)

        started_at = registry.earliest_file_created_at(batch_id=b.batch_id)
        ended_at = None
        if is_done:
            terminal_times = registry.file_terminal_times(batch_id=b.batch_id)
            ended_at = max(terminal_times.values()) if terminal_times else None

        rows.append(
            {
                "batch_id": b.batch_id,
                "state": label,
                "files": total,
                "completed": counts[FileState.COMPLETED],
                "failed": failed,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration": _format_duration(started_at, ended_at) if ended_at else None,
            }
        )

    recent_rows = [
        row for index, row in enumerate(rows) if index < 7 or row["batch_id"] in attention_batches
    ]
    st.caption("Recent batches and older batches needing attention. Select a row for details.")
    if len(recent_rows) < len(rows):
        st.toggle("Show all batches", key="batch_log_show_all")
    visible_rows = rows if st.session_state.get("batch_log_show_all") else recent_rows
    batch_ids = [str(row["batch_id"]) for row in visible_rows]
    mode = "all" if st.session_state.get("batch_log_show_all") else "recent"
    snapshot = sha256(json.dumps(batch_ids).encode()).hexdigest()[:16]
    table_key = f"batch_log_table_{mode}_{snapshot}"
    st.session_state["batch_log_active_table"] = table_key
    st.dataframe(
        visible_rows,
        on_select=lambda: _queue_batch_selection(table_key, batch_ids),
        selection_mode="single-row",
        column_config={
            "batch_id": st.column_config.TextColumn("Batch"),
            "state": st.column_config.TextColumn("State"),
            "files": st.column_config.NumberColumn("Files"),
            "completed": st.column_config.NumberColumn("Completed"),
            "failed": st.column_config.NumberColumn("Failed"),
            "started_at": st.column_config.DatetimeColumn("Started", format="YYYY-MM-DD HH:mm"),
            "ended_at": st.column_config.DatetimeColumn("Ended", format="YYYY-MM-DD HH:mm"),
            "duration": st.column_config.TextColumn("Duration"),
        },
        hide_index=True,
        height=min(330, 38 + 35 * len(visible_rows)),
        key=table_key,
    )
    clicked = st.session_state.pop("batch_log_navigation", None)
    if clicked in batch_ids and st.session_state.get("selected_batch") != clicked:
        st.session_state["selected_batch"] = clicked
        st.rerun()

def _queue_batch_selection(table_key: str, batch_ids: list[str]) -> None:
    if st.session_state.get("batch_log_active_table") != table_key:
        return
    rows = st.session_state[table_key]["selection"]["rows"]
    selection = (table_key, tuple(rows))
    if st.session_state.get("batch_log_last_selection") == selection:
        return
    st.session_state["batch_log_last_selection"] = selection
    if rows and 0 <= rows[0] < len(batch_ids):
        st.session_state["batch_log_navigation"] = batch_ids[rows[0]]

def _suggest_batch_id(registry: Registry) -> str:
    existing = {b.batch_id for b in registry.list_batches()}
    n = 1
    while f"BATCH{n:02d}" in existing:
        n += 1
    return f"BATCH{n:02d}"

def _close_add_batch() -> None:
    st.session_state["add_batch_open"] = False
    st.session_state.pop("add_batch_wizard", None)

@st.dialog("Start migration", width="large", on_dismiss=_close_add_batch)
def _add_batch_dialog(registry: Registry, settings: Settings) -> None:
    wiz = st.session_state.setdefault("add_batch_wizard", {"step": "select"})
    step = wiz.get("step", "select")
    if step == "select":
        _render_add_batch_select(registry, settings, wiz)
    elif step == "running":
        _render_add_batch_running(registry, settings, wiz)
    elif step == "handoff":
        _render_start_migration_handoff(wiz)
    else:
        _render_add_batch_done(wiz)

def _render_add_batch_select(registry: Registry, settings: Settings, wiz: _AddBatchWizard) -> None:
    destination = st.radio(
        "Take files to",
        list(BatchDestination),
        format_func=lambda d: (
            "Data Vault -- the migration (recommended)"
            if d is BatchDestination.VAULT
            else "Data Bridge only -- stop before archiving"
        ),
        horizontal=True,
        key="start_migration_destination",
        help=(
            "Data Vault is where the estate must land; Data Bridge is only the route "
            "in. Data Bridge only is for a trial or a staged cutover -- those files "
            "stay BRIDGED until someone archives them."
        ),
    )
    wiz["destination"] = str(destination)
    mode = st.radio(
        "Mode",
        ["Automated folder migration", "Advanced selected-file batch"],
        horizontal=True,
        key="start_migration_mode",
    )
    if mode == "Automated folder migration":
        _render_start_migration_automated(registry, settings, wiz)
        return
    _render_add_batch_manual_select(registry, settings, wiz)

def _render_start_migration_automated(
    registry: Registry, settings: Settings, wiz: _AddBatchWizard
) -> None:
    st.caption(
        "Default mode: run the full pipeline for one source folder -- discover, "
        "validate, upload, import, verify, archive, reconcile and sign off if clean."
    )
    source_text = str(
        st.text_input("Source folder", value=wiz.get("source_root", str(settings.source_root)))
    )
    wiz["source_root"] = source_text
    source_root = Path(source_text)
    derived_batch = derive_batch_id(source_root)

    destination = BatchDestination(str(wiz.get("destination", BatchDestination.VAULT)))
    preview_count: int | None = None
    new_files: list[MigrationFile] = []
    if not source_root.exists():
        st.warning(f"Folder not found: {source_root}")
    else:
        candidates = list(scanner.scan(source_root=source_root, pattern=settings.file_pattern))
        known = registry.known_source_paths(paths=[c.source_path for c in candidates])
        new_files = [c for c in candidates if c.source_path not in known]
        preview_count = len(new_files)
        hidden = len(candidates) - preview_count
        if hidden:
            st.caption(f"{hidden} file(s) already migrated or in progress elsewhere.")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Derived batch", derived_batch)
    with c2:
        st.metric("New files", preview_count if preview_count is not None else "-")
    with c3:
        st.metric("Workers", max(1, settings.max_concurrent_uploads))

    st.info(
        "This launches `migration-hub migrate --source <folder>` in the background. "
        "The dashboard tracks progress from the registry after the CLI creates or "
        "updates the derived batch."
    )
    bridge_only = destination is BatchDestination.BRIDGE
    if bridge_only:
        st.caption(
            "Data Bridge only: the files are registered and validated here, then "
            "`run` workers take them to BRIDGED and stop."
        )
    if st.button(
        "Start automated migration",
        type="primary",
        icon=":material/rocket_launch:",
        disabled=not source_root.exists() or (bridge_only and not new_files),
    ):
        if bridge_only:
            wiz["batch_id"] = derived_batch
            _start_add_batch(registry, settings, wiz, new_files)
            st.rerun()
        registry.ensure_batch(
            batch_id=derived_batch, max_concurrency=settings.max_concurrent_uploads
        )
        registry.set_batch_destination(batch_id=derived_batch, destination=destination)
        with st.status("Starting automated migration", expanded=True) as status:
            handle = batch_ops.start_migrate_source_subprocess(
                source_root=source_root, environment=settings.environment
            )
            st.write(f"Started `migration-hub migrate --source` for `{source_root}`.")
            st.write(f"Derived batch: `{derived_batch}`")
            st.write(f"Process id: `{handle.pid}`")
            st.write(f"Output: `{handle.log_path}`")
            status.update(label="Handed off to migrate", state="complete", expanded=False)

        wiz["step"] = "handoff"
        wiz["batch_id"] = derived_batch
        wiz["summary"] = {
            "pid": handle.pid,
            "log_path": str(handle.log_path),
            "source_root": str(source_root),
        }
        if registry.get_batch(derived_batch) is not None:
            st.session_state["selected_batch"] = derived_batch
        st.rerun()

def _render_add_batch_manual_select(
    registry: Registry, settings: Settings, wiz: _AddBatchWizard
) -> None:
    st.caption(
        "Advanced/manual mode: pick discovered files to migrate, then start the run."
    )

    batch_id = str(
        st.text_input("Batch id", value=wiz.get("batch_id") or _suggest_batch_id(registry))
    )
    wiz["batch_id"] = batch_id

    source_text = str(
        st.text_input("Source folder", value=wiz.get("source_root", str(settings.source_root)))
    )
    wiz["source_root"] = source_text
    source_root = Path(source_text)

    if not source_root.exists():
        st.warning(f"Folder not found: {source_root}")
        available: list[MigrationFile] = []
    else:
        candidates = list(scanner.scan(source_root=source_root, pattern=settings.file_pattern))
        known = registry.known_source_paths(paths=[c.source_path for c in candidates])
        available = [c for c in candidates if c.source_path not in known]
        hidden = len(candidates) - len(available)
        if hidden:
            st.caption(f"{hidden} file(s) already migrated or in progress elsewhere -- hidden.")

    selected_paths: list[str] = []
    if not available:
        st.info("No new files found in this folder.")
    else:
        rows = [
            {
                "file": c.source_database,
                "path": c.source_path,
                "size_mb": round(c.size_bytes / (1024 * 1024), 1),
            }
            for c in available
        ]
        event = st.dataframe(
            rows,
            on_select="rerun",
            selection_mode="multi-row",
            column_config={
                "file": st.column_config.TextColumn("File"),
                "path": st.column_config.TextColumn("Path"),
                "size_mb": st.column_config.NumberColumn("Size (MB)", format="%.1f"),
            },
            hide_index=True,
            key="add_batch_candidates",
        )
        selected_paths = [available[i].source_path for i in _selected_rows(event)]
        st.caption(f"{len(selected_paths)} of {len(available)} file(s) selected.")

    if st.button(
        "Start migration",
        type="primary",
        icon=":material/play_arrow:",
        disabled=not batch_id or not selected_paths,
    ):
        chosen = [f for f in available if f.source_path in set(selected_paths)]
        _start_add_batch(registry, settings, wiz, chosen)
        st.rerun()

def _start_add_batch(
    registry: Registry, settings: Settings, wiz: _AddBatchWizard, chosen: list[MigrationFile]
) -> None:
    batch_id = str(wiz["batch_id"])
    for file in chosen:
        file.batch_id = batch_id

    with st.status("Starting migration", expanded=True) as status:
        destination = BatchDestination(str(wiz.get("destination", BatchDestination.VAULT)))
        registry.ensure_batch(
            batch_id=batch_id,
            max_concurrency=settings.max_concurrent_uploads,
            destination=destination,
        )
        registry.set_batch_destination(batch_id=batch_id, destination=destination)
        registered = registry.register(chosen)
        st.write(f"Registered **{registered}** file(s).")

        st.write(":material/fact_check: Validating...")
        results = validator.validate_batch(
            registry=registry, batch_id=batch_id, compute_checksum=settings.compute_checksums
        )
        ok = sum(1 for r in results.values() if r.ok)
        st.write(f"**{ok}** validated, **{len(results) - ok}** rejected.")

        if batch_ops.state_of(registry=registry, batch_id=batch_id) is BatchState.PAUSED:
            batch_ops.resume(registry=registry, batch_id=batch_id)

        worker_count = max(1, settings.max_concurrent_uploads)
        st.write(
            f":material/rocket_launch: Starting work to **{_DESTINATION_LABEL[destination]}**..."
        )
        handles = batch_ops.start_to_destination(
            registry=registry,
            batch_id=batch_id,
            count=worker_count,
            environment=settings.environment,
        )
        pids = ", ".join(f"`{h.pid}`" for h in handles)
        st.write(f"Worker(s) started (pid {pids}) -- run independently of this dialog.")

        status.update(label="Handed off to worker", state="complete", expanded=False)

    wiz["step"] = "running"
    st.session_state["selected_batch"] = batch_id

def _render_add_batch_running(
    registry: Registry, settings: Settings, wiz: _AddBatchWizard
) -> None:
    _add_batch_progress(registry, settings, str(wiz["batch_id"]), wiz)

@st.fragment(run_every="3s")
def _add_batch_progress(
    registry: Registry, settings: Settings, batch_id: str, wiz: _AddBatchWizard
) -> None:
    state = batch_ops.state_of(registry=registry, batch_id=batch_id)
    counts = batch_ops.progress(registry=registry, batch_id=batch_id)
    destination = registry.batch_destination(batch_id)
    total = sum(counts.values())
    outstanding = _outstanding(counts, destination)
    completed = counts[FileState.COMPLETED]
    if destination is BatchDestination.BRIDGE:
        completed += counts[FileState.BRIDGED]

    with st.container(horizontal=True, vertical_alignment="center"):
        st.subheader(f"Batch {batch_id}")
        st.badge(state.value.title(), color=_BATCH_STATE_COLOR[state])

    st.progress(
        completed / total if total else 0.0,
        text=f"{completed} of {total} file(s) at {_DESTINATION_LABEL[destination]}",
    )
    st.html(_pipeline_tracker_html(_pipeline_nodes(counts)))

    with st.container(horizontal=True):
        if state is BatchState.PAUSED:
            if st.button("Resume", icon=":material/play_circle:"):
                batch_ops.resume(registry=registry, batch_id=batch_id)
                batch_ops.start_to_destination(
                    registry=registry,
                    batch_id=batch_id,
                    count=max(1, settings.max_concurrent_uploads),
                    environment=settings.environment,
                )
        else:
            if st.button("Pause", icon=":material/pause_circle:"):
                batch_ops.pause(registry=registry, batch_id=batch_id)

    if total and outstanding == 0:
        wiz["step"] = "done"
        wiz["summary"] = {
            "total": total,
            "completed": completed,
            "failed": counts[FileState.FAILED] + counts[FileState.ABANDONED],
            "archive_failed": counts[FileState.ARCHIVE_FAILED],
        }
        st.rerun()

def _render_add_batch_done(wiz: _AddBatchWizard) -> None:
    summary: dict[str, int] = wiz.get("summary") or {}
    failed = summary.get("failed", 0)
    archive_failed = summary.get("archive_failed", 0)
    total = summary.get("total", 0)
    completed = summary.get("completed", 0)

    if failed or archive_failed:
        st.warning(
            f"Batch {wiz['batch_id']} finished: **{completed}** completed, "
            f"**{failed}** failed or abandoned, "
            f"**{archive_failed}** still need archive attention out of {total}."
        )
    else:
        st.success(f"Batch {wiz['batch_id']} completed -- all **{total}** file(s) migrated.")

    if st.button("Close", type="primary", width="stretch"):
        _close_add_batch()
        st.rerun()

def _render_start_migration_handoff(wiz: _AddBatchWizard) -> None:
    summary: dict[str, object] = wiz.get("summary") or {}
    st.success(f"Migration handed off for batch `{wiz.get('batch_id')}`.")
    st.caption(
        "The CLI process keeps running independently of this dialog. The dashboard "
        "will show the batch once the registry has rows for it."
    )
    st.write(f"Source folder: `{summary.get('source_root', '-')}`")
    st.write(f"Process id: `{summary.get('pid', '-')}`")
    st.write(f"Output: `{summary.get('log_path', '-')}`")
    st.caption(
        "To watch it live -- one line per step and per vendor call, with the reason "
        "when something is refused -- close this and turn on **Live log** under the "
        "batch. Or, outside the browser, in PowerShell from the tree root:"
    )
    st.code(f"Get-Content \"{summary.get('log_path', '-')}\" -Wait -Tail 40", language="powershell")
    if st.button("Close", type="primary", width="stretch"):
        _close_add_batch()
        st.rerun()

def _pipeline_nodes(counts: dict[FileState, int]) -> list[dict[str, str]]:
    total = sum(counts.values())
    chain = [
        (
            "discover",
            "Discover",
            FileState.DISCOVERED,
            [
                FileState.VALIDATED,
                FileState.REJECTED,
                FileState.UPLOADING,
                FileState.UPLOADED,
                FileState.IMPORTING,
                FileState.VERIFYING,
                *_ARCHIVE_STAGE_STATES,
            ],
        ),
        (
            "validate",
            "Validate",
            FileState.VALIDATED,
            [
                FileState.REJECTED,
                FileState.UPLOADING,
                FileState.UPLOADED,
                FileState.IMPORTING,
                FileState.VERIFYING,
                *_ARCHIVE_STAGE_STATES,
            ],
        ),
        (
            "upload",
            "Upload",
            FileState.UPLOADING,
            [FileState.UPLOADED, FileState.IMPORTING, FileState.VERIFYING, *_ARCHIVE_STAGE_STATES],
        ),
        ("import", "Import", FileState.IMPORTING, [FileState.VERIFYING, *_ARCHIVE_STAGE_STATES]),
        ("verify", "Verify", FileState.VERIFYING, list(_ARCHIVE_STAGE_STATES)),
        (
            "archive",
            "Archive",
            FileState.ARCHIVING,
            [FileState.ARCHIVE_FAILED, FileState.COMPLETED],
        ),
    ]

    nodes = []
    for key, label, at_state, later_states in chain:
        at_count = counts[at_state]
        passed = sum(counts[s] for s in later_states)
        if key == "discover":
            status = "pending" if total == 0 else ("active" if at_count > 0 else "done")
            sub = f"{total} found" if total else "none yet"
        else:
            status = "active" if at_count > 0 else ("done" if passed > 0 else "pending")
            sub = f"{at_count} now" if at_count else ("clear" if status == "done" else "waiting")
        nodes.append({"key": key, "label": label, "status": status, "sub": sub})

    completed = counts[FileState.COMPLETED]
    nodes.append(
        {
            "key": "complete",
            "label": "Complete",
            "status": "done" if completed > 0 else "pending",
            "sub": f"{completed} done",
        }
    )
    return nodes

_ARCHIVE_STAGE_STATES = (
    FileState.BRIDGED,
    FileState.ARCHIVING,
    FileState.ARCHIVE_FAILED,
    FileState.COMPLETED,
)

def _pipeline_tracker_html(nodes: list[dict[str, str]]) -> str:
    palette = _TRACKER_PALETTE.get(st.context.theme.type or "light", _TRACKER_PALETTE["light"])
    cells = []
    for i, node in enumerate(nodes):
        status = node["status"]
        if status == "done":
            bg, border, fg, glyph = palette["ok"], palette["ok"], "#FFFFFF", "&#10003;"
        elif status == "active":
            bg, border, fg = palette["accent_wash"], palette["accent"], palette["accent"]
            glyph = str(i + 1)
        else:
            bg, border, fg = palette["surface"], palette["line_strong"], palette["faint"]
            glyph = str(i + 1)

        prev_done = i > 0 and nodes[i - 1]["status"] == "done"
        connector = palette["ok"] if prev_done else palette["line_strong"]
        pulse = (
            f"animation:mh-pulse 1.6s ease-in-out infinite;"
            f"box-shadow:0 0 0 4px {palette['accent_wash']};"
            if status == "active"
            else ""
        )

        cells.append(f"""
        <div class="mh-node">
          <div class="mh-connector"
               style="background:{connector if i > 0 else "transparent"}"></div>
          <div class="mh-dot"
               style="background:{bg};border-color:{border};color:{fg};{pulse}">{glyph}</div>
          <div class="mh-label" style="color:{palette["ink"]}">{html.escape(node["label"])}</div>
          <div class="mh-sub" style="color:{palette["faint"]}">{html.escape(node["sub"])}</div>
        </div>""")

    return f"""
    <style>
      .mh-track {{ display: flex; width: 100%; padding: 4px 0 2px; }}
      .mh-node {{
        flex: 1; position: relative;
        display: flex; flex-direction: column; align-items: center; gap: 6px;
      }}
      .mh-connector {{ position: absolute; top: 15px; left: -50%; width: 100%; height: 2px; }}
      .mh-dot {{
        width: 32px; height: 32px; border-radius: 50%; border: 2px solid;
        display: flex; align-items: center; justify-content: center;
        font-size: 13px; font-weight: 700; position: relative; z-index: 1;
      }}
      .mh-label {{ font-size: 12.5px; font-weight: 600; text-align: center; }}
      .mh-sub {{ font-size: 11px; text-align: center; font-family: monospace; }}
      @keyframes mh-pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}
    </style>
    <div class="mh-track">{"".join(cells)}</div>
    """

def _format_duration(start: datetime | None, end: datetime | None = None) -> str | None:
    if start is None:
        return None
    start = start if start.tzinfo else start.replace(tzinfo=UTC)
    end = end if end is not None else datetime.now(UTC)
    end = end if end.tzinfo else end.replace(tzinfo=UTC)
    seconds = max(0, int((end - start).total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"

def _format_timedelta(delta: timedelta | None) -> str:
    if delta is None:
        return "Insufficient measurement"
    seconds = max(0, int(delta.total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"

def _format_bytes(value: float | int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(amount) < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{amount:.0f} B"
        amount /= 1024
    return f"{amount:.1f} TB"

def _known_file_state(raw: str) -> FileState | None:
    try:
        return FileState(raw)
    except ValueError:
        return None

def _is_done(state: FileState, destination: BatchDestination) -> bool:
    return state in done_states(destination)

def _file_progress(raw: str, destination: BatchDestination = BatchDestination.VAULT) -> int:
    state = _known_file_state(raw)
    if state is None:
        return 0
    return 100 if _is_done(state, destination) else _STATE_PROGRESS[state]

def _file_state_color(
    raw: str, destination: BatchDestination = BatchDestination.VAULT
) -> _BadgeColor:
    state = _known_file_state(raw)
    if state is None:
        return "gray"
    if state in _PROBLEM_STATES:
        return "red"
    if _is_done(state, destination):
        return "green"
    if state is FileState.BRIDGED:
        return "orange"
    if state in (FileState.DISCOVERED, FileState.VALIDATED):
        return "gray"
    return "blue"

def _file_state_priority(raw: str, destination: BatchDestination = BatchDestination.VAULT) -> int:
    state = _known_file_state(raw)
    if state is None:
        return 5
    if state in _PROBLEM_STATES:
        return 1
    if _is_done(state, destination):
        return 4
    if state is FileState.BRIDGED:
        return 2
    if state in (FileState.DISCOVERED, FileState.VALIDATED):
        return 3
    return 0

def _file_table_style(
    rows: list[dict[str, object]],
    *,
    dark: bool,
    destination: BatchDestination = BatchDestination.VAULT,
) -> Styler:
    tones = {
        "green": ("#1F5B41", "#E4F3EA") if not dark else ("#8FDBB6", "#113023"),
        "orange": ("#7A4E0A", "#FBF0DF") if not dark else ("#EFC978", "#302610"),
        "red": ("#8A2C2C", "#FBE9E9") if not dark else ("#F1A9A4", "#33191A"),
        "blue": ("#141052", "#E8E7F2") if not dark else ("#7FEFF5", "#0E2A2D"),
        "gray": ("#3F4C57", "#F8F8F8") if not dark else ("#B7C2CA", "#161D24"),
    }

    def state_style(value: object) -> str:
        foreground, background = tones[_file_state_color(str(value), destination)]
        return f"color: {foreground}; background-color: {background}; font-weight: 600"

    return pd.DataFrame(rows).style.set_uuid("file_progress").map(state_style, subset=["state"])

def _file_table_rows(
    files: Sequence[MigrationFile],
    terminal_times: dict[int, datetime],
    destination: BatchDestination = BatchDestination.VAULT,
) -> list[dict[str, object]]:
    return [
        {
            "file_id": f.file_id,
            "source_database": f.source_database,
            "target_exposure_name": f.target_exposure_name,
            "state": f.state,
            "progress": _file_progress(f.state, destination),
            "size_mb": round(f.size_bytes / (1024 * 1024), 1),
            "attempts": f.attempts,
            "archive_attempts": f.archive_attempts,
            "started_at": f.created_at,
            "ended_at": terminal_times.get(f.file_id),
            "duration": _format_duration(f.created_at, terminal_times.get(f.file_id)),
            "last_error": f.last_error,
        }
        for f in sorted(
            files, key=lambda file: (_file_state_priority(file.state, destination), file.file_id)
        )
    ]

def _support_bundle_filename(file: MigrationFile) -> str:
    safe_name = "".join(
        ch if ch.isalnum() or ch in ("-", "_") else "_"
        for ch in file.source_database
    ).strip("_")
    return f"file_{file.file_id}_{safe_name or 'source'}.json"

def _support_bundle_json(settings: Settings, file_id: int) -> str:
    bundle = audit.export_for_support(
        engine=_engine(settings.database_url),
        file_id=file_id,
    )
    return json.dumps(bundle, indent=2, sort_keys=True)

def _render_retry_action(
    registry: Registry, settings: Settings, batch: str, files: Sequence[MigrationFile]
) -> None:
    retryable = [
        f
        for f in files
        if _known_file_state(f.state) in (FileState.FAILED, FileState.ABANDONED)
    ]
    if not retryable:
        return

    if st.button(f"Retry {len(retryable)} failed/abandoned file(s)", icon=":material/replay:"):
        with st.status("Retrying", expanded=True) as status:
            handle = batch_ops.start_retry_subprocess(
                batch_id=batch, environment=settings.environment
            )
            st.write(f"Retry started (pid `{handle.pid}`). Output: `{handle.log_path}`")
            _note_launch(batch, "Retry", handle)
            st.write(
                "Each file is checked in Data Vault, then Data Bridge: already in "
                "Data Vault -> COMPLETED; on Data Bridge -> archive only; on "
                "neither -> uploaded again. The retry then starts the pipeline "
                "for this batch itself -- nothing else to run. If files in the "
                "batch are still in flight it starts nothing, and says so in "
                "the log."
            )
            status.update(label="Handed off to the retry", state="complete", expanded=False)
        st.rerun()

def _render_archive_action(
    registry: Registry, settings: Settings, batch: str
) -> None:
    pending = registry.pending_archives(batch_id=batch)
    if not pending:
        return

    failed = sum(1 for f in pending if f.state == str(FileState.ARCHIVE_FAILED))
    st.warning(
        f"**{len(pending)} file(s) reached Data Bridge but are not in the "
        "Data Vault**"
        + (f", {failed} of them after a failed archive" if failed else "")
        + ". Data Bridge is the route in; the Vault is the destination. These "
        "are not migrated until they are archived."
    )
    if st.button(
        f"Archive {len(pending)} file(s) into Data Vault"
        + (" (retries the archive only)" if failed else ""),
        icon=":material/inventory_2:",
    ):
        with st.status("Archiving", expanded=True) as status:
            handle = batch_ops.start_archive_subprocess(
                batch_id=batch, environment=settings.environment
            )
            st.write(f"Archiver started (pid `{handle.pid}`).")
            _note_launch(batch, "Archive", handle)
            st.write(f"Output: `{handle.log_path}`")
            st.write(
                "Runs in the background -- this count falls as files are "
                "archived. Safe to press again: a file already archived is "
                "skipped, and a recorded archive job is seen through before "
                "another is started."
            )
            status.update(
                label="Handed off to the archiver", state="complete", expanded=False
            )

_IDLE_MINUTES = 5

_LIVE_LOG_LINES = 200

def _render_live_log(registry: Registry, batch: str) -> None:
    if not st.toggle(
        "Live log",
        key=f"live_log_{batch}",
        help="Follow this batch's run log as it is written -- refreshes every 2 seconds.",
    ):
        return
    _live_log_panel(registry, batch)

@st.fragment(run_every="2s")
def _live_log_panel(registry: Registry, batch: str) -> None:
    logs = batch_ops.logs_for_batch(batch)
    if not logs:
        st.info(
            "No log for this batch yet. Logs are written by runs started from this "
            "dashboard (Start migration, Resume, Continue, Retry, Archive); a run "
            "started from a terminal prints in that terminal instead."
        )
        return

    with st.container(horizontal=True, vertical_alignment="center"):
        chosen = st.selectbox(
            "Log",
            logs,
            format_func=lambda p: p.name,
            key=f"live_log_file_{batch}",
            label_visibility="collapsed",
        )
        problems_only = st.checkbox("Problems only", key=f"live_log_problems_{batch}")
    if chosen is None or not chosen.is_file():
        return

    lines = batch_ops.tail_lines(chosen, lines=_LIVE_LOG_LINES)
    problems = [ln for ln in lines if " WARNING " in ln or " ERROR " in ln]
    quiet_minutes = (datetime.now().timestamp() - chosen.stat().st_mtime) / 60
    outstanding = _outstanding(
        registry.counts_by_state(batch_id=batch), registry.batch_destination(batch)
    )

    caption = (
        f"{chosen.name} -- last written {_ago(quiet_minutes)}, "
        f"{len(problems)} warning/error line(s) in view"
    )
    if outstanding and quiet_minutes >= _IDLE_MINUTES:
        st.warning(
            f"{caption}. {outstanding} file(s) are outstanding but this log has been "
            f"quiet for {quiet_minutes:.0f} min -- the run may have ended or stalled."
        )
    else:
        st.caption(caption)

    shown = problems if problems_only else lines
    with st.container(height=420):
        st.code("\n".join(shown) or "(nothing yet)", language=None)

def _ago(minutes: float) -> str:
    if minutes < 1:
        return f"{minutes * 60:.0f} s ago"
    if minutes < 90:
        return f"{minutes:.0f} min ago"
    return f"{minutes / 60:.1f} h ago"

def _minutes_since_activity(registry: Registry, batch: str) -> float:
    stamps = [e.occurred_at for e in registry.recent_events(batch_id=batch, limit=1)]
    stamps += [t.occurred_at for t in registry.recent_transactions(batch_id=batch, limit=1)]
    if not stamps:
        return float("inf")
    newest = max(stamps)
    now = datetime.now(UTC).replace(tzinfo=None)
    return (now - newest).total_seconds() / 60

_LAUNCH_NOTES_KEPT = 3

def _note_launch(batch: str | None, label: str, handle: batch_ops.WorkerHandle) -> None:
    if not batch:
        return
    notes = st.session_state.setdefault("launch_notes", {}).setdefault(batch, [])
    notes.insert(
        0,
        {
            "label": label,
            "pid": handle.pid,
            "log": str(handle.log_path),
            "at": datetime.now().strftime("%H:%M:%S"),
        },
    )
    del notes[_LAUNCH_NOTES_KEPT:]

def _render_launch_notes(batch: str) -> None:
    notes = st.session_state.get("launch_notes", {}).get(batch) or []
    if not notes:
        return
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.caption("Recent actions -- follow them with **Live log** below.")
            if st.button("Dismiss", key=f"dismiss_launch_notes_{batch}", type="tertiary"):
                st.session_state["launch_notes"].pop(batch, None)
                st.rerun(scope="fragment")
        for note in notes:
            st.caption(
                f"{note['at']}  **{note['label']}** started -- pid `{note['pid']}`, "
                f"log `{note['log']}`"
            )

def _launch_continue(registry: Registry, settings: Settings, batch: str) -> None:
    destination = registry.batch_destination(batch)
    with st.status("Starting workers", expanded=True) as status:
        handles = batch_ops.start_to_destination(
            registry=registry,
            batch_id=batch,
            count=max(1, settings.max_concurrent_uploads),
            environment=settings.environment,
        )
        handle = handles[0]
        for h in handles:
            _note_launch(batch, f"Workers to {_DESTINATION_LABEL[destination]}", h)
        pids = ", ".join(f"`{h.pid}`" for h in handles)
        st.write(f"Started, to **{_DESTINATION_LABEL[destination]}** (pid {pids}).")
        st.write(
            "Watch it with **Live log** below, or in PowerShell from the tree root:"
        )
        st.code(f'Get-Content "{handle.log_path}" -Wait -Tail 40', language="powershell")
        status.update(label="Workers started", state="complete", expanded=True)

def _render_batch_action_bar(registry: Registry, settings: Settings, batch: str) -> None:
    counts = registry.counts_by_state(batch_id=batch)
    destination = registry.batch_destination(batch)
    total = sum(counts.values())
    outstanding = _outstanding(counts, destination)
    pending_archives = registry.pending_archives(batch_id=batch)
    retryable = registry.files_in_states(
        states=(FileState.FAILED, FileState.ABANDONED), batch_id=batch
    )
    issues = (
        counts[FileState.FAILED]
        + counts[FileState.ABANDONED]
        + counts[FileState.ARCHIVE_FAILED]
    )
    state = batch_ops.state_of(registry=registry, batch_id=batch)
    display_state, display_color = _batch_display_state(state, counts, destination)

    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.badge(display_state, color=display_color)
            st.badge(
                f"to {_DESTINATION_LABEL[destination]}",
                color="violet" if destination is BatchDestination.BRIDGE else "gray",
                icon=":material/flag:",
            )
            st.caption(
                f"{total} file(s), {outstanding} outstanding, "
                f"{len(pending_archives)} pending archive, {issues} issue(s)"
            )

        idle = outstanding > 0 and _minutes_since_activity(registry, batch) >= _IDLE_MINUTES
        with st.container(horizontal=True):
            if state is BatchState.PAUSED:
                if st.button("Resume", icon=":material/play_circle:", key=f"resume_{batch}"):
                    batch_ops.resume(registry=registry, batch_id=batch)
                    _launch_continue(registry, settings, batch)
            else:
                if st.button("Pause", icon=":material/pause_circle:", key=f"pause_{batch}"):
                    batch_ops.pause(registry=registry, batch_id=batch)
                    st.rerun()
                if idle and st.button(
                    f"Continue {outstanding} outstanding",
                    icon=":material/play_circle:",
                    key=f"continue_{batch}",
                    help=(
                        f"No worker activity for {_IDLE_MINUTES}+ minutes, so nothing is "
                        "working on this batch -- e.g. it was resumed after its workers "
                        "exited. Starts it again, as far as its destination."
                    ),
                ):
                    _launch_continue(registry, settings, batch)

            archive_label = f"Archive {len(pending_archives)} into Data Vault"
            if st.button(
                archive_label,
                icon=":material/inventory_2:",
                disabled=not pending_archives,
                key=f"archive_{batch}",
            ):
                with st.status("Archiving", expanded=True) as status:
                    handle = batch_ops.start_archive_subprocess(
                        batch_id=batch, environment=settings.environment
                    )
                    st.write(f"Archiver started (pid `{handle.pid}`).")
                    _note_launch(batch, "Archive", handle)
                    st.write(f"Output: `{handle.log_path}`")
                    status.update(
                        label="Handed off to the archiver", state="complete", expanded=False
                    )

            retry_label = f"Retry {len(retryable)} failed/abandoned"
            if st.button(
                retry_label,
                icon=":material/replay:",
                disabled=not retryable,
                key=f"retry_{batch}",
            ):
                with st.status("Retrying", expanded=True) as status:
                    handle = batch_ops.start_retry_subprocess(
                        batch_id=batch, environment=settings.environment
                    )
                    st.write(f"Retry started (pid `{handle.pid}`). Output: `{handle.log_path}`")
                    _note_launch(batch, "Retry", handle)
                    status.update(label="Handed off to the retry", state="complete", expanded=False)
                st.rerun()

def _render_tabs(registry: Registry, settings: Settings, batch: str) -> None:
    files_tab, activity_tab, controls_tab, diagnostics_tab = st.tabs(
        ["Files", "Activity", "Controls", "Health"]
    )

    with files_tab:
        files = registry.batch_files(batch_id=batch)
        if not files:
            st.caption("No files registered in this batch yet.")
        else:
            _render_archive_action(registry, settings, batch)
            _render_retry_action(registry, settings, batch, files)
            terminal_times = registry.file_terminal_times(batch_id=batch)
            destination = registry.batch_destination(batch)
            rows = _file_table_rows(files, terminal_times, destination)
            selected_key = f"selected_file_{batch}"
            file_by_id = {f.file_id: f for f in files}
            if st.session_state.get(selected_key) not in file_by_id:
                st.session_state.pop(selected_key, None)

            file_ids = [cast(int, row["file_id"]) for row in rows]
            snapshot = sha256(json.dumps(file_ids).encode()).hexdigest()[:16]
            table_key = f"files_table_{batch}_{snapshot}"
            st.session_state[f"active_files_table_{batch}"] = table_key
            st.dataframe(
                _file_table_style(
                    rows, dark=st.context.theme.type == "dark", destination=destination
                ),
                column_config={
                    "file_id": st.column_config.NumberColumn("ID", pinned=True),
                    "source_database": st.column_config.TextColumn("Source EDM"),
                    "target_exposure_name": st.column_config.TextColumn("Target exposure"),
                    "state": {**st.column_config.TextColumn("State"), "alignment": "center"},
                    "progress": st.column_config.ProgressColumn(
                        "Progress", min_value=0, max_value=100, format="%d%%"
                    ),
                    "size_mb": st.column_config.NumberColumn("Size (MB)", format="%.1f"),
                    "attempts": st.column_config.NumberColumn("Attempts"),
                    "archive_attempts": st.column_config.NumberColumn("Archive attempts"),
                    "started_at": {
                        **st.column_config.DatetimeColumn("Started", format="HH:mm:ss"),
                        "alignment": "center",
                    },
                    "ended_at": {
                        **st.column_config.DatetimeColumn("Ended", format="HH:mm:ss"),
                        "alignment": "center",
                    },
                    "duration": {
                        **st.column_config.TextColumn("Duration"),
                        "alignment": "center",
                    },
                    "last_error": {
                        **st.column_config.TextColumn("Last error"),
                        "alignment": "center",
                    },
                },
                hide_index=True,
                key=table_key,
                on_select=lambda: _select_file_from_table(table_key, batch, file_ids),
                selection_mode="single-row",
                height=min(390, 38 + 35 * len(rows)),
            )

            selected_file_id = st.session_state.get(selected_key)
            selected_file = (
                file_by_id.get(selected_file_id) if isinstance(selected_file_id, int) else None
            )
            if selected_file is not None:
                _render_file_detail_panel(registry, settings, selected_file)

    with activity_tab:
        _render_activity_tab(registry, batch)

    with controls_tab:
        _render_controls_tab(registry, settings, batch)

    with diagnostics_tab:
        _render_diagnostics_tab(registry, batch)

def _select_file_from_table(table_key: str, batch: str, file_ids: list[int]) -> None:
    if st.session_state.get(f"active_files_table_{batch}") != table_key:
        return
    rows = st.session_state[table_key]["selection"]["rows"]
    selection = (table_key, tuple(rows))
    last_selection_key = f"file_table_last_selection_{batch}"
    if st.session_state.get(last_selection_key) == selection:
        return
    st.session_state[last_selection_key] = selection
    if rows and 0 <= rows[0] < len(file_ids):
        st.session_state[f"selected_file_{batch}"] = file_ids[rows[0]]

def _display_value(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)

def _render_file_detail_panel(
    registry: Registry, settings: Settings, file: MigrationFile
) -> None:
    st.divider()
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader("Selected file")
            st.badge(
                file.state,
                color=_file_state_color(file.state, registry.batch_destination(file.batch_id)),
            )
            st.caption(f"ID {file.file_id}")

        metric_cols = st.columns(3)
        metric_cols[0].metric("Size", _format_bytes(file.size_bytes))
        metric_cols[1].metric("Attempts", str(file.attempts))
        metric_cols[2].metric("Archive attempts", str(file.archive_attempts))

        detail_rows = [
            ("Source database", file.source_database),
            ("Source path", file.source_path),
            ("Target exposure", file.target_exposure_name),
            ("Instance name", file.instance_name),
            ("Database name", file.database_name),
            ("Job id", file.job_id),
            ("Archive job id", file.archive_job_id),
            ("Uploaded at", file.uploaded_at),
            ("Archived at", file.archived_at),
            ("Archive expiration", file.archive_expiration_date),
            ("Last error", file.last_error),
        ]
        st.dataframe(
            [{"field": label, "value": _display_value(value)} for label, value in detail_rows],
            column_config={
                "field": st.column_config.TextColumn("Field"),
                "value": st.column_config.TextColumn("Value"),
            },
            hide_index=True,
            width="stretch",
        )

        state_tab, calls_tab, support_tab = st.tabs(["State history", "Vendor calls", "Support"])
        with state_tab:
            events = registry.recent_events(file_id=file.file_id, limit=500)
            if not events:
                st.caption("No state history recorded for this file.")
            else:
                st.dataframe(
                    [
                        {
                            "occurred_at": e.occurred_at,
                            "from_state": e.from_state or "-",
                            "to_state": e.to_state,
                            "actor": e.actor,
                            "detail": e.detail,
                        }
                        for e in events
                    ],
                    column_config={
                        "occurred_at": st.column_config.DatetimeColumn(
                            "Occurred", format="YYYY-MM-DD HH:mm:ss"
                        ),
                        "from_state": st.column_config.TextColumn("From"),
                        "to_state": st.column_config.TextColumn("To"),
                        "actor": st.column_config.TextColumn("Actor"),
                        "detail": st.column_config.TextColumn("Detail"),
                    },
                    hide_index=True,
                    width="stretch",
                )

        with calls_tab:
            transactions = registry.recent_transactions(file_id=file.file_id, limit=500)
            if not transactions:
                st.caption("No vendor calls recorded for this file.")
            else:
                st.dataframe(
                    [
                        {
                            "occurred_at": t.occurred_at,
                            "method": t.method,
                            "endpoint": metrics.endpoint_key(t.url),
                            "status": t.status_code,
                            "duration_ms": t.duration_ms,
                            "correlation_id": t.correlation_id,
                            "url": t.url,
                        }
                        for t in transactions
                    ],
                    column_config={
                        "occurred_at": st.column_config.DatetimeColumn(
                            "Occurred", format="YYYY-MM-DD HH:mm:ss"
                        ),
                        "method": st.column_config.TextColumn("Method"),
                        "endpoint": st.column_config.TextColumn("Endpoint"),
                        "status": st.column_config.NumberColumn("Status"),
                        "duration_ms": st.column_config.NumberColumn("Duration (ms)"),
                        "correlation_id": st.column_config.TextColumn("Correlation ID"),
                        "url": st.column_config.TextColumn("URL"),
                    },
                    hide_index=True,
                    width="stretch",
                )

        with support_tab:
            st.dataframe(
                [
                    {"identifier": "File id", "value": str(file.file_id)},
                    {"identifier": "Batch", "value": file.batch_id},
                    {"identifier": "Source database", "value": file.source_database},
                    {"identifier": "Target exposure", "value": file.target_exposure_name},
                    {"identifier": "Job id", "value": file.job_id or "-"},
                    {"identifier": "Archive job id", "value": file.archive_job_id or "-"},
                    {
                        "identifier": "Correlation IDs",
                        "value": _correlation_ids(registry, file.file_id),
                    },
                ],
                column_config={
                    "identifier": st.column_config.TextColumn("Identifier"),
                    "value": st.column_config.TextColumn("Value"),
                },
                hide_index=True,
                width="stretch",
            )

            state = _known_file_state(file.state)
            retryable = state in (
                FileState.FAILED,
                FileState.ABANDONED,
                FileState.ARCHIVE_FAILED,
            )
            action_cols = st.columns(2)
            with action_cols[0]:
                if st.button(
                    "Retry file",
                    icon=":material/replay:",
                    disabled=not retryable,
                    key=f"retry_file_{file.file_id}",
                ):
                    with st.status("Retrying file", expanded=True) as status:
                        handle = batch_ops.start_retry_subprocess(
                            file_id=file.file_id,
                            environment=settings.environment,
                        )
                        st.write(f"Retry started (pid `{handle.pid}`).")
                        st.write(f"Output: `{handle.log_path}`")
                        _note_launch(file.batch_id, f"Retry file {file.file_id}", handle)
                        status.update(
                            label="Handed off to the retry",
                            state="complete",
                            expanded=False,
                        )
            with action_cols[1]:
                try:
                    support_json = _support_bundle_json(settings, file.file_id)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.download_button(
                        "Export support bundle",
                        data=support_json,
                        file_name=_support_bundle_filename(file),
                        mime="application/json",
                        icon=":material/download:",
                        key=f"support_bundle_{file.file_id}",
                    )

def _correlation_ids(registry: Registry, file_id: int) -> str:
    ids = [
        str(t.correlation_id)
        for t in registry.recent_transactions(file_id=file_id, limit=500)
        if t.correlation_id
    ]
    return ", ".join(dict.fromkeys(ids)) or "-"

_PROBLEM_STATES = frozenset(
    {FileState.FAILED, FileState.ABANDONED, FileState.REJECTED, FileState.ARCHIVE_FAILED}
)

_ACTIVITY_ROWS_PER_FILE = 60

_ACTIVITY_MAX_FILES = 50

_ACTIVITY_WINDOWS: dict[str, int | None] = {
    "All time": None,
    "15 min": 15,
    "1 hour": 60,
    "6 hours": 360,
    "24 hours": 1440,
}

def _activity_tree(
    files: Sequence[MigrationFile],
    events: Sequence[MigrationEvent],
    transactions: Sequence[ApiTransaction],
    *,
    since: datetime | None = None,
    include_calls: bool = True,
    problems_only: bool = False,
) -> list[tuple[MigrationFile, list[dict[str, object]]]]:
    rows_by_file: dict[int, list[dict[str, object]]] = {f.file_id: [] for f in files}

    for e in events:
        if since is not None and e.occurred_at < since:
            continue
        restaged = e.from_state == FileState.UPLOADING and e.to_state == FileState.VALIDATED
        rows_by_file.setdefault(e.file_id, []).append(
            {
                "when": e.occurred_at,
                "step": "state",
                "what": f"{e.from_state or '-'} -> {e.to_state}",
                "result": e.to_state,
                "ms": None,
                "detail": e.detail or "",
                "problem": e.to_state in _PROBLEM_STATES or restaged,
            }
        )

    if include_calls:
        for t in transactions:
            if t.file_id is None or (since is not None and t.occurred_at < since):
                continue
            failed = t.status_code is None or t.status_code >= 400
            rows_by_file.setdefault(t.file_id, []).append(
                {
                    "when": t.occurred_at,
                    "step": "call",
                    "what": f"{t.method} {metrics.endpoint_key(t.url)}",
                    "result": str(t.status_code) if t.status_code is not None else "no response",
                    "ms": t.duration_ms,
                    "detail": audit.short_reason(t.response_body) if failed else "",
                    "problem": failed,
                }
            )

    tree: list[tuple[MigrationFile, list[dict[str, object]]]] = []
    for f in files:
        rows = sorted(rows_by_file.get(f.file_id, []), key=lambda r: str(r["when"]))
        if problems_only:
            rows = [r for r in rows if r["problem"]]
            if not rows and f.state not in _PROBLEM_STATES:
                continue
        elif since is not None and not rows:
            continue
        tree.append((f, rows[-_ACTIVITY_ROWS_PER_FILE:]))
    return tree

def _render_activity_tab(registry: Registry, batch: str) -> None:
    with st.container(horizontal=True, vertical_alignment="bottom"):
        window = st.selectbox(
            "Window", list(_ACTIVITY_WINDOWS), key=f"activity_window_{batch}", width=160
        )
        find = st.text_input(
            "Find file", key=f"activity_find_{batch}", placeholder="name contains...", width=260
        ).strip().lower()
        include_calls = st.checkbox(
            "Vendor calls", value=True, key=f"activity_calls_{batch}"
        )
        problems_only = st.checkbox("Problems only", key=f"activity_problems_{batch}")

    minutes = _ACTIVITY_WINDOWS[window]
    since = (
        datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes)
        if minutes is not None
        else None
    )
    files = [
        f
        for f in registry.batch_files(batch_id=batch)
        if not find or find in f.source_database.lower()
    ]
    tree = _activity_tree(
        files,
        registry.batch_events(batch_id=batch),
        registry.batch_transactions(batch_id=batch) if include_calls else [],
        since=since,
        include_calls=include_calls,
        problems_only=problems_only,
    )
    if not tree:
        st.caption("Nothing matches -- widen the window or clear the filters.")
        return
    if len(tree) > _ACTIVITY_MAX_FILES:
        st.caption(
            f"Showing the first {_ACTIVITY_MAX_FILES} of {len(tree)} files -- "
            "use Find file to narrow it."
        )

    for f, rows in tree[:_ACTIVITY_MAX_FILES]:
        state = f.state
        problems = sum(1 for r in rows if r["problem"])
        if state in _PROBLEM_STATES or problems:
            icon = ":material/error:"
        elif state == FileState.COMPLETED:
            icon = ":material/check_circle:"
        else:
            icon = ":material/pending:"
        label = (
            f"**#{f.file_id}  {f.source_database}**  ·  {state}  ·  "
            f"attempt {f.attempts}  ·  {len(rows)} step(s)"
            + (f"  ·  {problems} problem(s)" if problems else "")
        )
        open_by_default = state not in SETTLED_STATES or bool(problems)
        with st.expander(label, icon=icon, expanded=open_by_default):
            if f.last_error:
                st.caption(f"Last error: {f.last_error}")
            if not rows:
                st.caption("No activity in this window.")
                continue
            st.dataframe(
                [{k: v for k, v in r.items() if k != "problem"} for r in rows],
                column_config={
                    "when": st.column_config.DatetimeColumn(
                        "When", format="HH:mm:ss", width="small"
                    ),
                    "step": st.column_config.TextColumn("", width="small"),
                    "what": st.column_config.TextColumn("What", width="large"),
                    "result": st.column_config.TextColumn("Result", width="small"),
                    "ms": st.column_config.NumberColumn("ms", format="%d", width="small"),
                    "detail": st.column_config.TextColumn("Detail", width="large"),
                },
                hide_index=True,
                height=min(38 + 35 * len(rows), 400),
            )

def _render_controls_tab(registry: Registry, settings: Settings, batch: str) -> None:
    runs = registry.list_batch_runs(batch_id=batch)
    if not runs:
        st.subheader("No control run recorded")
        _render_controls_open_form(settings, batch)
        return

    selected_key = f"controls_selected_run_{batch}"
    run_by_id = {str(run.run_id): run for run in runs}
    if st.session_state.get(selected_key) not in run_by_id:
        st.session_state[selected_key] = str(runs[-1].run_id)
    selected_id = st.selectbox(
        "Control run",
        list(run_by_id),
        format_func=lambda run_id: f"Run {run_by_id[run_id].run_seq} - {run_by_id[run_id].trigger}",
        key=selected_key,
    )
    selected_run = run_by_id[selected_id]
    _render_selected_control_run_summary(registry, selected_run)
    _render_control_issues(registry, selected_run)
    _render_control_actions(registry, settings, selected_run)

    _, reasons = batch_ops.exit_criteria_met(registry=registry, batch_id=batch)
    with st.expander("Batch completion checks"):
        if reasons:
            for reason in reasons:
                st.write(reason)
        else:
            st.caption("All runs closed; latest run signed off.")

    rows: list[dict[str, object]] = []
    for run in runs:
        live = (
            controls.derive(registry=registry, run_id=run.run_id)
            if run.status == str(controls.RunStatus.RUNNING)
            else None
        )
        rows.append(
            {
                "run_seq": run.run_seq,
                "trigger": run.trigger,
                "status": run.status + (" (live)" if live else ""),
                "initiated_by": run.initiated_by,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "source_count": run.source_count,
                "target_count": live.target_count if live else run.target_count,
                "completed": live.completed_count if live else run.completed_count,
                "abandoned": live.abandoned_count if live else run.abandoned_count,
                "rejected": live.rejected_count if live else run.rejected_count,
                "archive_failed": live.archive_failed_count if live else run.archive_failed_count,
                "retry_count": live.retry_count if live else run.retry_count,
                "evidence": run.evidence_uri,
                "signed_off_by": run.signed_off_by,
                "run_id": str(run.run_id),
            }
        )

    with st.expander(f"Run history ({len(runs)})"):
        st.dataframe(
            rows,
            column_config={
                "run_id": st.column_config.TextColumn("Run id"),
                "run_seq": st.column_config.NumberColumn("Run"),
                "started_at": st.column_config.DatetimeColumn("Started", format="YYYY-MM-DD HH:mm"),
                "finished_at": st.column_config.DatetimeColumn(
                    "Finished", format="YYYY-MM-DD HH:mm"
                ),
                "source_count": st.column_config.NumberColumn("Source"),
                "target_count": st.column_config.NumberColumn("Target"),
                "retry_count": st.column_config.NumberColumn("Retries"),
                "signed_off_by": st.column_config.TextColumn("Signed off by"),
            },
            hide_index=True,
            height=min(285, 38 + 35 * len(rows)),
        )

def _render_selected_control_run_summary(registry: Registry, run: BatchRun) -> None:
    live = (
        controls.derive(registry=registry, run_id=run.run_id)
        if run.status == str(controls.RunStatus.RUNNING)
        else None
    )
    outstanding = live.outstanding_count if live else run.outstanding_count
    exceptions = live.exception_count if live else run.exception_count

    if run.status == str(controls.RunStatus.ABORTED):
        st.subheader("Control run aborted")
    elif run.signed_off_at is not None:
        st.subheader("Run signed off")
    elif live:
        st.subheader("Closure blocked" if outstanding else "Ready to close")
    elif run.status == str(controls.RunStatus.EXCEPTIONS):
        st.subheader("Closed with exceptions")
    else:
        st.subheader("Ready for sign-off")

    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            color: _BadgeColor = (
                "blue" if run.status == str(controls.RunStatus.RUNNING) else "green"
            )
            if run.status == str(controls.RunStatus.EXCEPTIONS):
                color = "orange"
            if run.status == str(controls.RunStatus.ABORTED):
                color = "red"
            st.badge(run.status + (" (live)" if live else ""), color=color)
            st.write(f"**Run {run.run_seq}** `{run.run_id}`")
        cols = st.columns(4)
        with cols[0]:
            st.metric("Outstanding", outstanding)
            st.caption(f"Trigger {run.trigger}")
        with cols[1]:
            st.metric("Exceptions", exceptions)
            st.caption(f"Retries {live.retry_count if live else run.retry_count}")
        with cols[2]:
            st.metric("Evidence", "yes" if run.evidence_uri else "no")
            st.caption(run.evidence_sha256 or "no hash recorded")
        with cols[3]:
            st.metric("Signed off", "yes" if run.signed_off_at else "no")
            st.caption(run.signed_off_by or "not signed")

        if run.evidence_uri:
            st.caption(f"Evidence: `{run.evidence_uri}`")
        if run.notes:
            st.caption(f"Notes: {run.notes}")

def _render_control_issues(registry: Registry, run: BatchRun) -> None:
    destination = registry.batch_destination(run.batch_id)
    settled = SETTLED_STATES | done_states(destination)
    files = registry.batch_files(batch_id=run.batch_id)
    running = run.status == str(controls.RunStatus.RUNNING)
    issues = [file for file in files if file.state not in settled or file.state in _PROBLEM_STATES]
    if not issues:
        return
    with st.expander(f"Current file issues ({len(issues)})", expanded=running):
        st.dataframe(
            [
                {
                    "ID": file.file_id,
                    "Source EDM": file.source_database,
                    "State": file.state,
                    "Closure": "Blocked" if file.state not in settled else "Settled exception",
                    "Recorded reason": file.last_error or "Not recorded",
                }
                for file in sorted(
                    issues,
                    key=lambda file: (_file_state_priority(file.state, destination), file.file_id),
                )
            ],
            hide_index=True,
            height=min(285, 38 + 35 * len(issues)),
        )
        if not running:
            st.caption("Current batch state; closed run totals remain frozen.")

def _render_control_actions(registry: Registry, settings: Settings, run: BatchRun) -> None:
    status = controls.RunStatus(run.status)
    outstanding = (
        controls.derive(registry=registry, run_id=run.run_id).outstanding_count
        if status is controls.RunStatus.RUNNING
        else run.outstanding_count
    )
    run_id = str(run.run_id)

    if status is controls.RunStatus.RUNNING:
        if outstanding:
            st.caption(f"{outstanding} unsettled file(s); closure requires no outstanding work.")
        with st.container(horizontal=True):
            if st.button(
                "Close run",
                icon=":material/check_circle:",
                key=f"close_run_{run_id}",
                help="Reconcile and freeze totals. The CLI rechecks closure eligibility.",
            ):
                _launch_control_command(
                    "Close run",
                    batch_ops.start_controls_close_subprocess(
                        run_id=run_id, environment=settings.environment, batch_id=run.batch_id
                    ),
                    "Refresh after completion; closed totals and reconciliation will appear here.",
                )
            _render_abort_control_form(settings, run_id, run.batch_id)
        return

    if status is controls.RunStatus.EXCEPTIONS and run.signed_off_at is None:
        st.caption(
            "Run-level acceptance only. File outcomes stay unchanged; "
            "individual accept/ignore decisions are not yet supported."
        )
    missing_reasons = (
        [
            file.file_id
            for file in registry.files_in_states(
                states=[FileState.ABANDONED, FileState.ARCHIVE_FAILED], batch_id=run.batch_id
            )
            if not file.last_error
        ]
        if status is controls.RunStatus.EXCEPTIONS
        else []
    )
    if missing_reasons and run.signed_off_at is None:
        st.caption(f"Sign-off blocked: no recorded reason for file IDs {missing_reasons}.")
    with st.container(horizontal=True):
        _render_export_control_form(settings, run)
        if st.button("Verify totals", icon=":material/fact_check:", key=f"verify_run_{run_id}"):
            _launch_control_command(
                "Verify run",
                batch_ops.start_controls_verify_subprocess(
                    run_id=run_id, environment=settings.environment, batch_id=run.batch_id
                ),
                "Refresh after completion; divergences, if any, are in the command log.",
            )
        signed = run.signed_off_at is not None
        if st.button(
            "Sign off",
            icon=":material/approval:",
            disabled=signed or status is controls.RunStatus.ABORTED or bool(missing_reasons),
            help="Accept this closed run. The CLI marks the batch DONE only when all checks pass.",
            key=f"sign_off_run_{run_id}",
        ):
            st.session_state[f"sign_off_form_{run_id}"] = True
    if run.signed_off_at is not None:
        st.caption(f"Already signed off by {run.signed_off_by or 'unknown'}.")
    elif status is controls.RunStatus.ABORTED:
        st.caption("Aborted runs are recorded with a reason rather than signed off.")

    if (
        st.session_state.get(f"sign_off_form_{run_id}")
        and not signed
        and status is not controls.RunStatus.ABORTED
        and not missing_reasons
    ):
        _render_sign_off_control_form(settings, run_id, run.batch_id)

def _render_controls_open_form(settings: Settings, batch: str) -> None:
    with st.form(f"controls_open_{batch}", border=True):
        st.subheader("Open control run")
        trigger = st.selectbox(
            "Trigger",
            [controls.RunTrigger.RUN, controls.RunTrigger.RETRY, controls.RunTrigger.RESUME],
            format_func=lambda value: value.value,
        )
        by = st.text_input("Initiated by", value=_default_operator())
        submitted = st.form_submit_button("Open run", icon=":material/add_circle:")
    if submitted:
        _launch_control_command(
            "Open run",
            batch_ops.start_controls_open_subprocess(
                batch_id=batch,
                trigger=trigger,
                by=by.strip() or None,
                environment=settings.environment,
            ),
            "Refresh after completion; the new control run will appear in this tab.",
        )

def _render_abort_control_form(settings: Settings, run_id: str, batch: str) -> None:
    with st.popover("Abort control record", icon=":material/cancel:"):
        st.caption("The migration worker is not stopped by this action.")
        reason = st.text_area("Reason", key=f"abort_reason_{run_id}")
        if st.button(
            "Abort run",
            icon=":material/cancel:",
            disabled=not reason.strip(),
            key=f"abort_run_{run_id}",
        ):
            _launch_control_command(
                "Abort run",
                batch_ops.start_controls_abort_subprocess(
                    run_id=run_id,
                    reason=reason.strip(),
                    environment=settings.environment,
                    batch_id=batch,
                ),
                "Refresh after completion; the run will be marked ABORTED with this reason.",
            )

def _render_export_control_form(settings: Settings, run: BatchRun) -> None:
    run_id = str(run.run_id)
    default = Path("results") / "controls" / f"{run.batch_id}_run{run.run_seq}.csv"
    with st.popover("Export evidence", icon=":material/download:"):
        output_text = st.text_input("Output path", value=str(run.evidence_uri or default)).strip()
        if st.button(
            "Export",
            icon=":material/download:",
            disabled=not output_text,
            key=f"export_run_{run_id}",
        ):
            _launch_control_command(
                "Export evidence",
                batch_ops.start_controls_export_subprocess(
                    run_id=run_id,
                    output_path=Path(output_text),
                    environment=settings.environment,
                    batch_id=run.batch_id,
                ),
                "Refresh after completion; evidence path and SHA-256 will appear here.",
            )

def _render_sign_off_control_form(settings: Settings, run_id: str, batch: str) -> None:
    with st.form(f"sign_off_{run_id}", border=True):
        st.write(f"**Confirm sign-off: {batch}**")
        by = st.text_input("Operator", value=_default_operator())
        left, right = st.columns(2)
        submitted = left.form_submit_button(
            "Sign off run",
            icon=":material/approval:",
        )
        cancelled = right.form_submit_button("Cancel")
    if cancelled:
        st.session_state.pop(f"sign_off_form_{run_id}", None)
        st.rerun()
    if submitted:
        if not by.strip():
            st.error("Operator is required for sign-off.")
            return
        _launch_control_command(
            "Sign off run",
            batch_ops.start_controls_sign_off_subprocess(
                run_id=run_id, by=by.strip(), environment=settings.environment, batch_id=batch
            ),
            "Refresh after completion; sign-off and possible batch DONE status will appear here.",
        )
        st.session_state.pop(f"sign_off_form_{run_id}", None)

def _launch_control_command(
    label: str, handle: batch_ops.WorkerHandle, expected: str
) -> None:
    _note_launch(st.session_state.get("selected_batch"), label, handle)
    with st.status(label, expanded=True) as status:
        st.write(f"Started (pid `{handle.pid}`).")
        st.write(f"Output: `{handle.log_path}`")
        st.code(f'Get-Content "{handle.log_path}" -Wait -Tail 40', language="powershell")
        st.caption(expected)
        status.update(label=f"{label} handed off", state="complete", expanded=True)

def _default_operator() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""

def _render_diagnostics_tab(registry: Registry, batch: str) -> None:
    counts = registry.counts_by_state(batch_id=batch)
    pending_archive = len(registry.pending_archives(batch_id=batch))
    destination = registry.batch_destination(batch)
    outstanding = _outstanding(counts, destination)
    failures = (
        counts[FileState.FAILED]
        + counts[FileState.ABANDONED]
        + counts[FileState.ARCHIVE_FAILED]
        + counts[FileState.REJECTED]
    )
    quiet = _minutes_since_activity(registry, batch)

    st.subheader("Batch health")
    if failures:
        st.badge(f"{failures} file issue(s)", color="red", icon=":material/error:")
    if outstanding and quiet >= _IDLE_MINUTES:
        st.badge("No recent activity", color="orange", icon=":material/schedule:")
        st.caption(
            "Outstanding work with no recent registry activity; worker status is unconfirmed."
        )
    elif not outstanding and not failures and not pending_archive:
        st.badge("No open file issues", color="green", icon=":material/check_circle:")

    cols = st.columns(4)
    tiles = [
        ("Outstanding", outstanding, "not in a settled state"),
        ("Pending archive", pending_archive, "on Data Bridge, not in Vault"),
        ("Failures / rejected", failures, "needs review or disposition"),
        (
            "Last activity",
            _ago(quiet) if quiet != float("inf") else "never",
            "state change or vendor call",
        ),
    ]
    for col, (label, value, caption) in zip(cols, tiles, strict=True):
        with col, st.container(border=True):
            st.metric(label, str(value))
            st.caption(caption)

    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption("Files by state:")
        for state in FileState:
            if counts[state]:
                st.badge(f"{state} {counts[state]}", color=_file_state_color(state, destination))

    st.space("small")
    _render_operational_observability(registry, batch_id=batch)

if __name__ == "__main__":
    render()
