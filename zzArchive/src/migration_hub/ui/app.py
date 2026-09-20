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
from migration_hub.core.models import MigrationFile
from migration_hub.core.registry import Registry
from migration_hub.core.states import TERMINAL_STATES, BatchState, FileState
from migration_hub.orchestration import batches as batch_ops
from migration_hub.producers import scanner, stager, validator

load_dotenv()

_BadgeColor = Literal[
    "red", "orange", "yellow", "blue", "green", "violet", "gray", "grey", "primary"
]

_AddBatchWizard = dict[str, object]

_STATE_PROGRESS = {
    FileState.DISCOVERED: 10,
    FileState.VALIDATED: 25,
    FileState.REJECTED: 0,
    FileState.STAGED: 40,
    FileState.UPLOADING: 55,
    FileState.UPLOADED: 65,
    FileState.IMPORTING: 78,
    FileState.VERIFYING: 90,
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
    outstanding = total - sum(counts[s] for s in TERMINAL_STATES)
    if state in (BatchState.PLANNED, BatchState.RUNNING) and total > 0 and outstanding == 0:
        failed = counts[FileState.FAILED] + counts[FileState.ABANDONED]
        return ("Completed with issues", "orange") if failed else ("Completed", "green")
    return state.value.title(), _BATCH_STATE_COLOR[state]

_TRACKER_PALETTE = {
    "light": {
        "surface": "#FFFFFF",
        "line_strong": "#C3CDD5",
        "ink": "#0F1720",
        "faint": "#8695A1",
        "ok": "#2E7D5B",
        "accent": "#0E7C86",
        "accent_wash": "#E3F1F1",
    },
    "dark": {
        "surface": "#11161C",
        "line_strong": "#313D46",
        "ink": "#E7ECEF",
        "faint": "#5D6C77",
        "ok": "#57B98A",
        "accent": "#35C6C1",
        "accent_wash": "#102B2A",
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
            "Add batch", type="primary", icon=":material/add:", width="stretch"
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
        st.info("No batches yet. Click **+ Add batch** above to start one.")
        return

    st.subheader(f"Batch {selected}")
    _render_tabs(registry, settings, selected)

@st.fragment(run_every="8s")
def _render_overview(registry: Registry) -> None:
    _kpi_strip_global(registry)
    st.space("small")
    _render_batch_log(registry)

def _kpi_strip_global(registry: Registry) -> None:
    counts = registry.counts_by_state(batch_id=None)
    in_flight = (
        counts[FileState.UPLOADING] + counts[FileState.IMPORTING] + counts[FileState.VERIFYING]
    )
    issues = counts[FileState.FAILED] + counts[FileState.ABANDONED]
    completed_recently = registry.count_transitions_since(
        batch_id=None,
        to_state=FileState.COMPLETED,
        since=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
    )

    tiles = [
        ("gray", "Discovered", counts[FileState.DISCOVERED], "awaiting validation"),
        ("gray", "Staged", counts[FileState.STAGED], "ready to upload"),
        ("blue", "In flight", in_flight, "uploading · importing · verifying"),
        (
            "green",
            "Completed",
            counts[FileState.COMPLETED],
            f"+{completed_recently} in the last hour"
            if completed_recently
            else "none in the last hour",
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

def _render_batch_log(registry: Registry) -> None:
    batches = registry.list_batches()
    if not batches:
        st.caption("No batches yet -- click **+ Add batch** to start one.")
        return

    rows = []
    for b in batches:
        counts = registry.counts_by_state(batch_id=b.batch_id)
        label, _color = _batch_display_state(BatchState(b.state), counts)

        total = sum(counts.values())
        outstanding = total - sum(counts[s] for s in TERMINAL_STATES)
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
                "failed": counts[FileState.FAILED] + counts[FileState.ABANDONED],
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

@st.dialog("Add batch", width="large", on_dismiss=_close_add_batch)
def _add_batch_dialog(registry: Registry, settings: Settings) -> None:
    wiz = st.session_state.setdefault("add_batch_wizard", {"step": "select"})
    step = wiz.get("step", "select")
    if step == "select":
        _render_add_batch_select(registry, settings, wiz)
    elif step == "running":
        _render_add_batch_running(registry, wiz)
    else:
        _render_add_batch_done(wiz)

def _render_add_batch_select(registry: Registry, settings: Settings, wiz: _AddBatchWizard) -> None:
    st.caption("Pick which discovered files to migrate, then start the run.")

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

        st.write(":material/inventory_2: Staging...")
        staging = stager.stage_batch(
            registry=registry,
            staging_root=settings.staging_root,
            batch_id=batch_id,
            stage_locally=settings.stage_locally,
            max_attempts=settings.max_attempts,
        )
        st.write(f"**{staging.staged}** file(s) staged.")
        if staging.failures:
            st.warning(
                f"**{len(staging.failures)}** file(s) failed staging and are now "
                "FAILED, retryable below:\n\n"
                + "\n".join(f"- `{f.name}` — {f.error}" for f in staging.failures)
            )

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

def _render_add_batch_running(registry: Registry, wiz: _AddBatchWizard) -> None:
    _add_batch_progress(registry, str(wiz["batch_id"]), wiz)

@st.fragment(run_every="3s")
def _add_batch_progress(registry: Registry, batch_id: str, wiz: _AddBatchWizard) -> None:
    state = batch_ops.state_of(registry=registry, batch_id=batch_id)
    counts = batch_ops.progress(registry=registry, batch_id=batch_id)
    total = sum(counts.values())
    outstanding = total - sum(counts[s] for s in TERMINAL_STATES)
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
        else:
            if st.button("Pause", icon=":material/pause_circle:"):
                batch_ops.pause(registry=registry, batch_id=batch_id)

    if total and outstanding == 0:
        wiz["step"] = "done"
        wiz["summary"] = {
            "total": total,
            "completed": completed,
            "failed": counts[FileState.FAILED] + counts[FileState.ABANDONED],
        }
        st.rerun()

def _render_add_batch_done(wiz: _AddBatchWizard) -> None:
    summary: dict[str, int] = wiz.get("summary") or {}
    failed = summary.get("failed", 0)
    total = summary.get("total", 0)
    completed = summary.get("completed", 0)

    if failed:
        st.warning(
            f"Batch {wiz['batch_id']} finished: **{completed}** completed, "
            f"**{failed}** failed or abandoned out of {total}."
        )
    else:
        st.success(f"Batch {wiz['batch_id']} completed -- all **{total}** file(s) migrated.")

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
                FileState.STAGED,
                FileState.UPLOADING,
                FileState.UPLOADED,
                FileState.IMPORTING,
                FileState.VERIFYING,
                FileState.COMPLETED,
            ],
        ),
        (
            "validate",
            "Validate",
            FileState.VALIDATED,
            [
                FileState.REJECTED,
                FileState.STAGED,
                FileState.UPLOADING,
                FileState.UPLOADED,
                FileState.IMPORTING,
                FileState.VERIFYING,
                FileState.COMPLETED,
            ],
        ),
        (
            "stage",
            "Stage",
            FileState.STAGED,
            [
                FileState.UPLOADING,
                FileState.UPLOADED,
                FileState.IMPORTING,
                FileState.VERIFYING,
                FileState.COMPLETED,
            ],
        ),
        (
            "upload",
            "Upload",
            FileState.UPLOADING,
            [FileState.UPLOADED, FileState.IMPORTING, FileState.VERIFYING, FileState.COMPLETED],
        ),
        ("import", "Import", FileState.IMPORTING, [FileState.VERIFYING, FileState.COMPLETED]),
        ("verify", "Verify", FileState.VERIFYING, [FileState.COMPLETED]),
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

def _render_retry_action(
    registry: Registry, settings: Settings, batch: str, files: Sequence[MigrationFile]
) -> None:
    retryable = [
        f for f in files if FileState(f.state) in (FileState.FAILED, FileState.ABANDONED)
    ]
    if not retryable:
        return

    if st.button(f"Retry {len(retryable)} failed/abandoned file(s)", icon=":material/replay:"):
        with st.status("Requeuing", expanded=True) as status:
            outcome = batch_ops.retry_files(
                registry,
                batch_id=batch,
                file_ids=[f.file_id for f in retryable],
                actor="operator",
                detail="manual retry from dashboard",
                worker_count=max(1, settings.max_concurrent_uploads),
                max_files_per_worker=None,
                environment=settings.environment,
            )
            st.write(f"Requeued **{len(outcome.requeued_file_ids)}** file(s) to STAGED.")

            pids = ", ".join(f"`{h.pid}`" for h in outcome.workers)
            st.write(f"Worker(s) started (pid {pids}).")
            status.update(
                label="Requeued and handed off to a worker", state="complete", expanded=False
            )
        st.rerun()

def _render_tabs(registry: Registry, settings: Settings, batch: str) -> None:
    files_tab, activity_tab = st.tabs(["Files", "Activity"])

    with files_tab:
        files = registry.files_in_states(states=list(FileState), batch_id=batch)
        if not files:
            st.caption("No files registered in this batch yet.")
        else:
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
                        "size_bytes": f.size_bytes,
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
                    "size_bytes": st.column_config.NumberColumn("Size", format="compact"),
                    "attempts": st.column_config.NumberColumn("Attempts"),
                    "started_at": st.column_config.DatetimeColumn("Started", format="HH:mm:ss"),
                    "ended_at": st.column_config.DatetimeColumn("Ended", format="HH:mm:ss"),
                    "duration": st.column_config.TextColumn("Duration"),
                    "last_error": st.column_config.TextColumn("Last error"),
                },
                hide_index=True,
            )

    with activity_tab:
        events_view, calls_view = st.tabs(["State transitions", "Vendor calls"])
        with events_view:
            events = registry.recent_events(batch_id=batch, limit=30)
            if not events:
                st.caption("No events yet.")
            for e in events:
                with st.container(border=True, horizontal=True, vertical_alignment="center"):
                    st.caption(e.occurred_at.strftime("%H:%M:%S"))
                    detail = f"  ·  {e.detail}" if e.detail else ""
                    st.write(f"`#{e.file_id}` {e.from_state or '—'} → **{e.to_state}**{detail}")

        with calls_view:
            st.caption("Bodies are already redacted at rest -- safe to display.")
            transactions = registry.recent_transactions(batch_id=batch, limit=30)
            if not transactions:
                st.caption("No vendor calls recorded yet.")
            else:
                st.dataframe(
                    [
                        {
                            "occurred_at": t.occurred_at,
                            "file_id": t.file_id,
                            "method": t.method,
                            "url": t.url,
                            "status_code": t.status_code,
                            "duration_ms": t.duration_ms,
                        }
                        for t in transactions
                    ],
                    column_config={
                        "occurred_at": st.column_config.DatetimeColumn("When", format="HH:mm:ss"),
                        "status_code": st.column_config.NumberColumn("Status"),
                        "duration_ms": st.column_config.NumberColumn("Duration", format="%d ms"),
                    },
                    hide_index=True,
                )

if __name__ == "__main__":
    render()
