from __future__ import annotations

from collections.abc import Iterable

import streamlit as st

def _badge_pair() -> None:
    with st.container(horizontal=True):
        st.badge("Blue = exists today", color="blue")
        st.badge("Green = target build", color="green")
        st.badge("Orange = action or risk", color="orange")

def _section_title(title: str, badge: str, color: str = "green") -> None:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.subheader(title)
        st.badge(badge, color=color)

def _metric_card(
    label: str, value: str | int, caption: str, *, status: str, color: str
) -> None:
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"**{label}**")
            st.badge(status, color=color)
        st.metric(label=label, value=value, label_visibility="collapsed")
        st.caption(caption)

def _disabled_button(label: str, *, kind: str = "secondary", icon: str | None = None) -> None:
    st.button(label, type=kind, icon=icon, disabled=True)

def _button_row(buttons: Iterable[tuple[str, str, str | None]]) -> None:
    with st.container(horizontal=True):
        for label, kind, icon in buttons:
            _disabled_button(label, kind=kind, icon=icon)

def render() -> None:
    st.set_page_config(page_title="Migration Hub UI/UX Mockup", layout="wide")

    st.title("Migration Hub UI/UX Mockup")
    st.caption(
        "Native Streamlit mockup for TASK-0076. Blue marks what exists today; "
        "green marks the target design to build. This page does not launch any "
        "migration actions."
    )
    _badge_pair()

    st.divider()
    _render_command_centre()
    st.divider()
    _render_start_migration()
    st.divider()
    _render_batch_detail()
    st.divider()
    _render_health_and_diagnostics()

def _render_command_centre() -> None:
    _section_title("Command Centre", "Target first viewport", "green")

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        _metric_card("Discovered", 48, "Awaiting validation", status="Today", color="blue")
    with c2:
        _metric_card("In flight", 7, "Upload, import, verify, archive", status="Today", color="blue")
    with c3:
        _metric_card("Completed", 312, "+18 in the last hour", status="Today", color="blue")
    with c4:
        _metric_card("Pending archive", 3, "On Data Bridge, not in Vault", status="Target", color="green")
    with c5:
        _metric_card("Needs action", 9, "Archive, retry, close or sign off", status="Target", color="green")

    left, right = st.columns([1, 1])
    with left:
        with st.container(border=True):
            _section_title("Needs Action Queue", "Target", "green")
            st.dataframe(
                [
                    {
                        "priority": "High",
                        "batch": "BATCH07",
                        "finding": "3 files need archive",
                        "next_action": "Open Files tab",
                    },
                    {
                        "priority": "High",
                        "batch": "BATCH07",
                        "finding": "4 failed or abandoned files",
                        "next_action": "Review and retry",
                    },
                    {
                        "priority": "Medium",
                        "batch": "BATCH07",
                        "finding": "1 run ready for close/sign-off",
                        "next_action": "Open Controls tab",
                    },
                ],
                hide_index=True,
                width="stretch",
            )
            _button_row(
                [
                    ("Open selected batch", "primary", ":material/open_in_new:"),
                    ("Show issues only", "secondary", ":material/filter_alt:"),
                ]
            )

    with right:
        with st.container(border=True):
            _section_title("Batch Log", "Today plus target columns", "blue")
            st.dataframe(
                [
                    {
                        "batch": "BATCH07",
                        "state": "Completed with issues",
                        "files": 114,
                        "completed": 108,
                        "failed": 4,
                        "pending_archive": 3,
                        "latest_control": "EXCEPTIONS",
                        "duration": "2h 18m",
                    },
                    {
                        "batch": "BATCH08",
                        "state": "Running",
                        "files": 64,
                        "completed": 21,
                        "failed": 0,
                        "pending_archive": 0,
                        "latest_control": "RUNNING",
                        "duration": "41m",
                    },
                    {
                        "batch": "BATCH09",
                        "state": "Planned",
                        "files": 0,
                        "completed": 0,
                        "failed": 0,
                        "pending_archive": 0,
                        "latest_control": "-",
                        "duration": "-",
                    },
                ],
                hide_index=True,
                width="stretch",
            )

def _render_start_migration() -> None:
    _section_title("Start Migration", "Target default with today advanced mode", "green")

    automated, manual = st.columns(2)
    with automated:
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown("**Default: automated folder migration**")
                st.badge("Target", color="green")
            st.text_input(
                "Source folder",
                value=r"\\SourceSqlServer\EDM_Backups\Wave07",
                disabled=True,
                key="mock_source_folder",
            )
            a, b, c = st.columns(3)
            with a:
                st.metric("Derived batch", "WAVE07")
            with b:
                st.metric("Candidate files", "114")
            with c:
                st.metric("Run mode", "Automated")
            st.caption(
                "Launches `migration-hub migrate --source <folder>` as a background "
                "CLI subprocess. The registry remains the source of truth."
            )
            st.progress(0.0, text="Ready to start: discover, validate, upload, import, verify, archive, reconcile, sign off if clean")
            _button_row(
                [
                    ("Start automated migration", "primary", ":material/rocket_launch:"),
                    ("Preview files", "secondary", ":material/search:"),
                ]
            )

    with manual:
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown("**Advanced: selected-file batch**")
                st.badge("Today", color="blue")
            st.text_input("Batch id", value="BATCH10", disabled=True, key="mock_batch_id")
            st.text_input(
                "Source folder",
                value=r"\\SourceSqlServer\EDM_Backups\Adhoc",
                disabled=True,
                key="mock_manual_source",
            )
            st.dataframe(
                [
                    {"selected": True, "file": "EDM_A_20260923.bak", "size_mb": 90540.0},
                    {"selected": True, "file": "RDM_B_20260923.bak", "size_mb": 47120.0},
                    {"selected": False, "file": "EDM_C_20260923.bak", "size_mb": 1840.0},
                ],
                hide_index=True,
                width="stretch",
            )
            st.caption("Registers selected files, validates them, then starts run workers.")
            _button_row(
                [
                    ("Start selected-file migration", "secondary", ":material/play_arrow:"),
                    ("Clear selection", "secondary", ":material/backspace:"),
                ]
            )

def _render_batch_detail() -> None:
    _section_title("Selected Batch Detail: BATCH07", "Target persistent action bar", "green")

    with st.container(border=True):
        a, b, c, d = st.columns(4)
        with a:
            st.markdown("**Operational state**")
            st.badge("Completed with issues", color="orange")
        with b:
            st.metric("Pending archive", 3)
        with c:
            st.metric("Exceptions", 4)
        with d:
            st.markdown("**Latest control**")
            st.badge("EXCEPTIONS - not signed", color="orange")
        _button_row(
            [
                ("Archive 3", "primary", ":material/inventory_2:"),
                ("Retry 4", "secondary", ":material/replay:"),
                ("Pause", "secondary", ":material/pause_circle:"),
                ("Refresh", "secondary", ":material/refresh:"),
            ]
        )

    files_tab, activity_tab, controls_tab, diagnostics_tab = st.tabs(
        ["Files", "Activity", "Controls", "Diagnostics"]
    )
    with files_tab:
        _render_files_tab()
    with activity_tab:
        _render_activity_tab()
    with controls_tab:
        _render_controls_tab()
    with diagnostics_tab:
        _render_diagnostics_tab()

def _render_files_tab() -> None:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.badge("Files tab exists today", color="blue")
        st.badge("Filters and row detail are target", color="green")

    f1, f2, f3, f4 = st.columns(4)
    with f1:
        st.selectbox("State", ["All states", "Failed/abandoned", "Pending archive", "Completed"], disabled=True)
    with f2:
        st.toggle("Issues only", value=True, disabled=True)
    with f3:
        st.toggle("In flight", value=False, disabled=True)
    with f4:
        st.toggle("Pending archive", value=True, disabled=True)

    st.dataframe(
        [
            {
                "id": 391,
                "source": "EDM_Claims_SE",
                "target": "EDM_Claims_SE_20260923",
                "state": "ARCHIVE_FAILED",
                "progress": 85,
                "attempts": 2,
                "last_error": "Archive job timed out",
                "next_action": "Open detail",
            },
            {
                "id": 392,
                "source": "EDM_Property_UK",
                "target": "EDM_Property_UK_20260923",
                "state": "COMPLETED",
                "progress": 100,
                "attempts": 1,
                "last_error": "",
                "next_action": "Open detail",
            },
            {
                "id": 393,
                "source": "RDM_Global",
                "target": "RDM_Global_20260923",
                "state": "ABANDONED",
                "progress": 0,
                "attempts": 3,
                "last_error": "Import job failed after retries",
                "next_action": "Retry file",
            },
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "progress": st.column_config.ProgressColumn("Progress", min_value=0, max_value=100)
        },
    )

    st.markdown("**Target file detail drawer**")
    detail, history = st.columns([1, 1])
    with detail:
        with st.container(border=True):
            st.badge("Target", color="green")
            st.markdown("**#391 EDM_Claims_SE**")
            st.caption("Target exposure: EDM_Claims_SE_20260923")
            st.dataframe(
                [
                    {"field": "Instance", "value": "eu-west-amlin-sql-01"},
                    {"field": "Database", "value": "EDM_Claims_SE"},
                    {"field": "Import job", "value": "job_7f31..."},
                    {"field": "Archive job", "value": "arch_44d2..."},
                    {"field": "Last error", "value": "Archive job timed out"},
                ],
                hide_index=True,
                width="stretch",
            )
            _button_row(
                [
                    ("Retry file", "primary", ":material/replay:"),
                    ("Export support bundle", "secondary", ":material/download:"),
                ]
            )
    with history:
        with st.container(border=True):
            st.badge("Target", color="green")
            st.markdown("**State and vendor history**")
            st.dataframe(
                [
                    {"when": "10:02:11", "type": "state", "detail": "VERIFYING to BRIDGED"},
                    {"when": "10:04:40", "type": "state", "detail": "BRIDGED to ARCHIVING"},
                    {"when": "10:34:40", "type": "vendor", "detail": "GET archive status -> 504"},
                    {"when": "10:34:40", "type": "state", "detail": "ARCHIVING to ARCHIVE_FAILED"},
                ],
                hide_index=True,
                width="stretch",
            )

def _render_activity_tab() -> None:
    events_view, calls_view = st.tabs(["State transitions", "Vendor calls"])
    with events_view:
        st.badge("Today", color="blue")
        st.dataframe(
            [
                {"when": "10:34:40", "file": 391, "transition": "ARCHIVING to ARCHIVE_FAILED", "detail": "Archive job timed out"},
                {"when": "10:04:40", "file": 391, "transition": "BRIDGED to ARCHIVING", "detail": "Archive started"},
                {"when": "10:02:11", "file": 392, "transition": "ARCHIVING to COMPLETED", "detail": "Confirmed in Data Vault"},
            ],
            hide_index=True,
            width="stretch",
        )
    with calls_view:
        st.badge("Today", color="blue")
        st.dataframe(
            [
                {"when": "10:34:40", "file": 391, "method": "GET", "url": "/archives", "status": 504, "duration_ms": 30000},
                {"when": "10:04:40", "file": 391, "method": "POST", "url": "/archives", "status": 202, "duration_ms": 812},
            ],
            hide_index=True,
            width="stretch",
        )

def _render_controls_tab() -> None:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.badge("Target", color="green")
        st.caption("Control records, reconciliation status and evidence are currently CLI-only.")

    st.dataframe(
        [
            {
                "run": 1,
                "trigger": "RUN",
                "status": "EXCEPTIONS",
                "source": "114",
                "completed": "108",
                "abandoned": "3",
                "rejected": "1",
                "archive_failed": "2",
                "bridge_count": "2",
                "evidence": "Batch07_run1.csv",
                "sign_off": "Pending",
            },
            {
                "run": 2,
                "trigger": "RETRY",
                "status": "RUNNING",
                "source": "114",
                "completed": "110 live",
                "abandoned": "2 live",
                "rejected": "1",
                "archive_failed": "1 live",
                "bridge_count": "-",
                "evidence": "-",
                "sign_off": "-",
            },
        ],
        hide_index=True,
        width="stretch",
    )
    _button_row(
        [
            ("Close run", "primary", ":material/check_circle:"),
            ("Export evidence", "secondary", ":material/download:"),
            ("Verify", "secondary", ":material/fact_check:"),
            ("Sign off", "secondary", ":material/approval:"),
            ("Abort with reason", "secondary", ":material/cancel:"),
        ]
    )
    st.info(
        "Close, sign-off and export should follow the same subprocess boundary as "
        "archive/retry where vendor reconciliation or formal CLI behaviour is involved."
    )

def _render_diagnostics_tab() -> None:
    st.badge("Target", color="green")
    st.dataframe(
        [
            {"check": "Outstanding files", "value": 0, "source": "registry.outstanding"},
            {"check": "Pending archives", "value": 3, "source": "registry.pending_archives"},
            {"check": "Recent failed/rejected", "value": 5, "source": "files_in_states"},
            {"check": "Recent 4xx/5xx vendor calls", "value": 2, "source": "recent_transactions"},
            {"check": "Stale claims", "value": 0, "source": "registry.stale_claims"},
        ],
        hide_index=True,
        width="stretch",
    )

def _render_health_and_diagnostics() -> None:
    _section_title("Health and Deferred Metrics", "Target plus guardrails", "green")
    health, diagnostics, deferred = st.columns(3)
    with health:
        with st.container(border=True):
            st.markdown("**Health strip**")
            st.badge("Target", color="green")
            st.dataframe(
                [
                    {"item": "Registry", "status": "Ready"},
                    {"item": "Environment", "status": "DEV"},
                    {"item": "Dry run", "status": "false"},
                    {"item": "API ready", "status": "OK"},
                ],
                hide_index=True,
                width="stretch",
            )
    with diagnostics:
        with st.container(border=True):
            st.markdown("**Operator diagnostics**")
            st.badge("Target", color="green")
            st.caption("One place for runbook triage signals before escalating or retrying.")
            _button_row(
                [
                    ("View stuck work", "secondary", ":material/search:"),
                    ("Show vendor errors", "secondary", ":material/bug_report:"),
                ]
            )
    with deferred:
        with st.container(border=True):
            st.markdown("**Throughput / ETA**")
            st.badge("Deferred", color="orange")
            st.warning(
                "Do not show ETA until `observability/metrics.py` implements "
                "`throughput()`, `projected_completion()` and `failure_rate()`."
            )

render()
