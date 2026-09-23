from __future__ import annotations

import html
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from migration_hub.config.settings import Settings
from migration_hub.core.models import ApiTransaction, MigrationEvent, MigrationFile
from migration_hub.core.registry import Registry
from migration_hub.core.states import SETTLED_STATES, BatchState, FileState
from migration_hub.observability import audit, controls, metrics
from migration_hub.orchestration import batches as batch_ops
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
    state: BatchState, counts: dict[FileState, int]
) -> tuple[str, _BadgeColor]:
    total = sum(counts.values())
    outstanding = total - sum(counts[s] for s in SETTLED_STATES)
    if state in (BatchState.PLANNED, BatchState.RUNNING) and total > 0 and outstanding == 0:
        failed = (
            counts[FileState.FAILED]
            + counts[FileState.ABANDONED]
            + counts[FileState.ARCHIVE_FAILED]
        )
        return ("Completed with issues", "orange") if failed else ("Completed", "green")
    return state.value.title(), _BATCH_STATE_COLOR[state]

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
        st.badge(settings.environment.upper(), color=env_color, icon=":material/dns:")
        if settings.dry_run:
            st.badge("Dry run", color="gray", icon=":material/science:")
        if st.button("Refresh now", icon=":material/refresh:"):
            st.rerun()
    if settings.environment == "prod":
        st.error("Production -- every action here is real.")

    if st.session_state.get("add_batch_open"):
        _add_batch_dialog(registry, settings)

    _render_overview(registry)
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
    _render_batch_action_bar(registry, settings, batch)

@st.fragment(run_every=_BATCH_REFRESH)
def _live_batch_tabs(registry: Registry, settings: Settings, batch: str) -> None:
    _render_tabs(registry, settings, batch)

@st.fragment(run_every="8s")
def _render_overview(registry: Registry) -> None:
    _render_unknown_states(registry)
    _kpi_strip_global(registry)
    st.space("small")
    _render_operational_observability(registry, batch_id=None)
    st.space("small")
    _render_needs_action_queue(registry)
    st.space("small")
    _render_batch_log(registry)

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
    with st.container(horizontal=True):
        for swatch, label, value, sub in tiles:
            with st.container(border=True):
                st.metric(f":{swatch}[■] {label}", value)
                st.caption(sub)

def _render_operational_observability(registry: Registry, *, batch_id: str | None) -> None:
    left, right = st.columns(2)
    with left:
        _render_throughput_panel(registry, batch_id=batch_id)
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
            eta_text = _format_timedelta(eta) if eta is not None else "Not enough data"
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
        counts = registry.counts_by_state(batch_id=batch.batch_id)
        pending_archive = len(registry.pending_archives(batch_id=batch.batch_id))
        failed = (
            counts[FileState.FAILED]
            + counts[FileState.ABANDONED]
            + counts[FileState.ARCHIVE_FAILED]
        )
        outstanding = sum(counts.values()) - sum(counts[s] for s in SETTLED_STATES)

        if pending_archive:
            rows.append(
                {
                    "priority": 1,
                    "batch": batch.batch_id,
                    "finding": f"{pending_archive} file(s) pending archive",
                    "next_action": "Archive into Data Vault",
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

    if not rows:
        with st.container(border=True):
            st.success("No batches need operator action right now.")
        return

    rows = sorted(rows, key=lambda r: (int(r["priority"]), str(r["batch"])))[:8]
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader("Needs action")
            st.badge("Target workflow", color="green", icon=":material/playlist_add_check:")
        st.caption("Open a batch below to inspect files, actions and controls.")
        for i, row in enumerate(rows):
            with st.container(horizontal=True, vertical_alignment="center"):
                color: _BadgeColor = "orange" if int(row["priority"]) <= 2 else "blue"
                st.badge(str(row["finding"]), color=color)
                st.write(f"**{row['batch']}** - {row['next_action']}")
                if st.button(
                    "Open",
                    key=f"needs_action_open_{i}_{row['batch']}",
                    icon=":material/open_in_new:",
                ):
                    st.session_state["selected_batch"] = row["batch"]
                    st.rerun()

def _render_batch_log(registry: Registry) -> None:
    batches = registry.list_batches()
    if not batches:
        st.caption("No batches yet -- click **Start migration** to start one.")
        return

    rows = []
    for b in batches:
        counts = registry.counts_by_state(batch_id=b.batch_id)
        label, _color = _batch_display_state(BatchState(b.state), counts)

        total = sum(counts.values())
        outstanding = total - sum(counts[s] for s in SETTLED_STATES)
        is_done = total > 0 and outstanding == 0

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
                "failed": counts[FileState.FAILED]
                + counts[FileState.ABANDONED]
                + counts[FileState.ARCHIVE_FAILED],
                "started_at": started_at,
                "ended_at": ended_at,
                "duration": _format_duration(started_at, ended_at) if ended_at else None,
            }
        )

    st.caption("Select a row to view that batch's files and activity below.")
    event = st.dataframe(
        rows,
        on_select="rerun",
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
        key="batch_log_table",
    )
    selected_rows = _selected_rows(event)
    if selected_rows:
        clicked = rows[selected_rows[0]]["batch_id"]
        if st.session_state.get("selected_batch") != clicked:
            st.session_state["selected_batch"] = clicked
            st.rerun()

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

    preview_count: int | None = None
    if not source_root.exists():
        st.warning(f"Folder not found: {source_root}")
    else:
        candidates = list(scanner.scan(source_root=source_root, pattern=settings.file_pattern))
        known = registry.known_source_paths(paths=[c.source_path for c in candidates])
        preview_count = len([c for c in candidates if c.source_path not in known])
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
    if st.button(
        "Start automated migration",
        type="primary",
        icon=":material/rocket_launch:",
        disabled=not source_root.exists(),
    ):
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
        registry.ensure_batch(batch_id=batch_id, max_concurrency=settings.max_concurrent_uploads)
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
            f":material/rocket_launch: Starting {worker_count} worker(s) for "
            "upload / import / verify..."
        )
        handles = batch_ops.start_run_subprocesses(
            registry=registry,
            batch_id=batch_id,
            count=worker_count,
            max_files=None,
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
    total = sum(counts.values())
    outstanding = total - sum(counts[s] for s in SETTLED_STATES)
    completed = counts[FileState.COMPLETED]

    with st.container(horizontal=True, vertical_alignment="center"):
        st.subheader(f"Batch {batch_id}")
        st.badge(state.value.title(), color=_BATCH_STATE_COLOR[state])

    st.progress(
        completed / total if total else 0.0,
        text=f"{completed} of {total} file(s) migrated",
    )
    st.html(_pipeline_tracker_html(_pipeline_nodes(counts)))

    with st.container(horizontal=True):
        if state is BatchState.PAUSED:
            if st.button("Resume", icon=":material/play_circle:"):
                batch_ops.resume(registry=registry, batch_id=batch_id)
                batch_ops.start_run_subprocesses(
                    registry=registry,
                    batch_id=batch_id,
                    count=max(1, settings.max_concurrent_uploads),
                    max_files=None,
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

def _render_retry_action(
    registry: Registry, settings: Settings, batch: str, files: Sequence[MigrationFile]
) -> None:
    retryable = [
        f for f in files if FileState(f.state) in (FileState.FAILED, FileState.ABANDONED)
    ]
    if not retryable:
        return

    if st.button(f"Retry {len(retryable)} failed/abandoned file(s)", icon=":material/replay:"):
        with st.status("Retrying", expanded=True) as status:
            handle = batch_ops.start_retry_subprocess(
                batch_id=batch, environment=settings.environment
            )
            st.write(f"Retry started (pid `{handle.pid}`). Output: `{handle.log_path}`")
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
    outstanding = len(registry.outstanding(batch_id=batch))

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

def _launch_continue(settings: Settings, batch: str) -> None:
    with st.status("Starting workers", expanded=True) as status:
        handle = batch_ops.start_migrate_subprocess(
            batch_id=batch, environment=settings.environment
        )
        st.write(f"`migration-hub migrate --batch {batch}` started (pid `{handle.pid}`).")
        st.write(
            "Watch it with **Live log** below, or in PowerShell from the tree root:"
        )
        st.code(f'Get-Content "{handle.log_path}" -Wait -Tail 40', language="powershell")
        status.update(label="Workers started", state="complete", expanded=True)

def _render_batch_action_bar(registry: Registry, settings: Settings, batch: str) -> None:
    counts = registry.counts_by_state(batch_id=batch)
    total = sum(counts.values())
    outstanding = total - sum(counts[s] for s in SETTLED_STATES)
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
    display_state, display_color = _batch_display_state(state, counts)

    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.badge(display_state, color=display_color)
            st.caption(
                f"{total} file(s), {outstanding} outstanding, "
                f"{len(pending_archives)} pending archive, {issues} issue(s)"
            )

        idle = outstanding > 0 and _minutes_since_activity(registry, batch) >= _IDLE_MINUTES
        with st.container(horizontal=True):
            if state is BatchState.PAUSED:
                if st.button("Resume", icon=":material/play_circle:", key=f"resume_{batch}"):
                    batch_ops.resume(registry=registry, batch_id=batch)
                    _launch_continue(settings, batch)
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
                        "exited. Starts `migrate --batch` for it."
                    ),
                ):
                    _launch_continue(settings, batch)

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
                    status.update(label="Handed off to the retry", state="complete", expanded=False)
                st.rerun()

def _render_tabs(registry: Registry, settings: Settings, batch: str) -> None:
    files_tab, activity_tab, controls_tab, diagnostics_tab = st.tabs(
        ["Files", "Activity", "Controls", "Diagnostics"]
    )

    with files_tab:
        files = registry.files_in_states(states=list(FileState), batch_id=batch)
        if not files:
            st.caption("No files registered in this batch yet.")
        else:
            _render_archive_action(registry, settings, batch)
            _render_retry_action(registry, settings, batch, files)
            terminal_times = registry.file_terminal_times(batch_id=batch)
            st.dataframe(
                [
                    {
                        "file_id": f.file_id,
                        "source_database": f.source_database,
                        "target_exposure_name": f.target_exposure_name,
                        "state": f.state,
                        "progress": _STATE_PROGRESS[FileState(f.state)],
                        "size_mb": round(f.size_bytes / (1024 * 1024), 1),
                        "attempts": f.attempts,
                        "started_at": f.created_at,
                        "ended_at": terminal_times.get(f.file_id),
                        "duration": _format_duration(f.created_at, terminal_times.get(f.file_id)),
                        "last_error": f.last_error,
                    }
                    for f in files
                ],
                column_config={
                    "file_id": st.column_config.NumberColumn("ID", pinned=True),
                    "source_database": st.column_config.TextColumn("Source EDM"),
                    "target_exposure_name": st.column_config.TextColumn("Target exposure"),
                    "state": st.column_config.TextColumn("State"),
                    "progress": st.column_config.ProgressColumn(
                        "Progress", min_value=0, max_value=100, format="%d%%"
                    ),
                    "size_mb": st.column_config.NumberColumn("Size (MB)", format="%.1f"),
                    "attempts": st.column_config.NumberColumn("Attempts"),
                    "started_at": st.column_config.DatetimeColumn("Started", format="HH:mm:ss"),
                    "ended_at": st.column_config.DatetimeColumn("Ended", format="HH:mm:ss"),
                    "duration": st.column_config.TextColumn("Duration"),
                    "last_error": st.column_config.TextColumn("Last error"),
                },
                hide_index=True,
            )

    with activity_tab:
        _render_activity_tab(registry, batch)

    with controls_tab:
        _render_controls_tab(registry, batch)

    with diagnostics_tab:
        _render_diagnostics_tab(registry, batch)

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
        to_state = FileState(e.to_state)
        restaged = e.from_state == FileState.UPLOADING and to_state is FileState.VALIDATED
        rows_by_file.setdefault(e.file_id, []).append(
            {
                "when": e.occurred_at,
                "step": "state",
                "what": f"{e.from_state or '-'} -> {e.to_state}",
                "result": e.to_state,
                "ms": None,
                "detail": e.detail or "",
                "problem": to_state in _PROBLEM_STATES or restaged,
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
            if not rows and FileState(f.state) not in _PROBLEM_STATES:
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
        state = FileState(f.state)
        problems = sum(1 for r in rows if r["problem"])
        if state in _PROBLEM_STATES or problems:
            icon = ":material/error:"
        elif state is FileState.COMPLETED:
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

def _render_controls_tab(registry: Registry, batch: str) -> None:
    runs = registry.list_batch_runs(batch_id=batch)
    with st.container(horizontal=True, vertical_alignment="center"):
        st.badge("Target workflow", color="green", icon=":material/fact_check:")
        st.caption("Formal control actions are visible here; wiring them is the next slice.")

    if not runs:
        st.info("No control records have been opened for this batch yet.")
        return

    rows = []
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
            }
        )

    st.dataframe(
        rows,
        column_config={
            "run_seq": st.column_config.NumberColumn("Run"),
            "started_at": st.column_config.DatetimeColumn("Started", format="YYYY-MM-DD HH:mm"),
            "finished_at": st.column_config.DatetimeColumn("Finished", format="YYYY-MM-DD HH:mm"),
            "source_count": st.column_config.NumberColumn("Source"),
            "target_count": st.column_config.NumberColumn("Target"),
            "retry_count": st.column_config.NumberColumn("Retries"),
            "signed_off_by": st.column_config.TextColumn("Signed off by"),
        },
        hide_index=True,
    )

    with st.container(horizontal=True):
        st.button("Close run", icon=":material/check_circle:", disabled=True)
        st.button("Export evidence", icon=":material/download:", disabled=True)
        st.button("Verify", icon=":material/fact_check:", disabled=True)
        st.button("Sign off", icon=":material/approval:", disabled=True)
        st.button("Abort with reason", icon=":material/cancel:", disabled=True)

def _render_diagnostics_tab(registry: Registry, batch: str) -> None:
    counts = registry.counts_by_state(batch_id=batch)
    pending_archive = len(registry.pending_archives(batch_id=batch))
    outstanding = len(registry.outstanding(batch_id=batch))
    failures = (
        counts[FileState.FAILED]
        + counts[FileState.ABANDONED]
        + counts[FileState.ARCHIVE_FAILED]
        + counts[FileState.REJECTED]
    )
    quiet = _minutes_since_activity(registry, batch)

    cols = st.columns(4)
    tiles = [
        ("Outstanding", outstanding, "not in a settled state"),
        ("Pending archive", pending_archive, "on Data Bridge, not in Vault"),
        ("Failures / rejected", failures, "needs review or disposition"),
        (
            "Last activity",
            _ago(quiet) if quiet != float("inf") else "never",
            "state change or vendor call"
            + (" -- nothing is working on it" if outstanding and quiet >= _IDLE_MINUTES else ""),
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
                color: _BadgeColor = (
                    "red"
                    if state in _PROBLEM_STATES
                    else "green"
                    if state is FileState.COMPLETED
                    else "blue"
                )
                st.badge(f"{state} {counts[state]}", color=color)

    st.space("small")
    _render_operational_observability(registry, batch_id=batch)

if __name__ == "__main__":
    render()
