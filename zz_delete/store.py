#!/usr/bin/env python3
"""
store.py
========

SQLite persistence for reconciliation runs. No Streamlit import, so it is
testable on its own.

Why a database at all, for what looks like a two-file diff: a reconciliation is
a statement about the platform *at a moment*. Keeping each load as a snapshot,
and each comparison as a run against two named snapshots, is what lets somebody
answer "was this database missing last week too, or is this new?" -- which is a
different and more useful question than "is it missing now". A throwaway view
answers only the second.

Snapshots are immutable once written. Re-running a comparison creates a new run;
it never edits an old one.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from reconcile import Outcome, Reconciliation, ResultRow, Row

#: Default location: beside the tool, not in the repo root. Gitignored -- a
#: reconciliation contains estate database names and belongs on the machine that
#: ran it, not in version control.
DEFAULT_DB_PATH = Path(__file__).with_name("reconcile.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id          INTEGER PRIMARY KEY,
    side        TEXT    NOT NULL CHECK (side IN ('master', 'platform', 'vault')),
    label       TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    key_column  TEXT    NOT NULL DEFAULT '',
    loaded_at   TEXT    NOT NULL,
    row_count   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_row (
    id          INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    data        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshot_row_snapshot ON snapshot_row(snapshot_id);

CREATE TABLE IF NOT EXISTS run (
    id                 INTEGER PRIMARY KEY,
    master_snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
    target_snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
    vault_snapshot_id  INTEGER          REFERENCES snapshot(id),
    ran_at             TEXT    NOT NULL,
    note               TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS run_result (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    outcome     TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    side        TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    data        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_result_run ON run_result(run_id);
"""


def _now() -> str:
    """UTC, ISO-8601. Runs get compared across machines and timezones; a naive
    local timestamp would make that comparison quietly wrong."""
    return datetime.now(UTC).isoformat(timespec="seconds")


#: The three lists a reconciliation compares. "platform" is what landed via
#: Data Bridge; "vault" is what is actually archived in Data Vault. They are
#: different claims -- see GLOSSARY.md and open question 13.
SIDES = ("master", "platform", "vault")


@dataclass(frozen=True)
class SnapshotInfo:
    id: int
    side: str
    label: str
    source: str
    loaded_at: str
    row_count: int
    key_column: str = ""


@dataclass(frozen=True)
class RunInfo:
    id: int
    master_snapshot_id: int
    target_snapshot_id: int
    ran_at: str
    note: str
    vault_snapshot_id: int | None = None
    master_label: str = ""
    target_label: str = ""


class Store:
    """A reconciliation database.

    Pass `":memory:"` for a throwaway one, which is what the tests use.
    """

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with closing(self._conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def save_snapshot(
        self,
        side: str,
        label: str,
        source: str,
        rows: Sequence[Row],
        key_column: str = "",
    ) -> int:
        """Store one loaded file. Returns the snapshot id.

        `source` records where the rows came from -- a filename today, an API
        endpoint once TASK-0054 lands. It is free text on purpose: the point is
        that a reader can tell, not that this tool can parse it.

        `key_column` records which column was matched on. A run compared on
        exposureSetName and one compared on databaseName are not comparable,
        and without this nothing would say which was used.
        """
        if side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}, not {side!r}")
        if not key_column and rows:
            key_column = rows[0].data.get("_key_column", "")
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO snapshot "
                "(side, label, source, key_column, loaded_at, row_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (side, label, source, key_column, _now(), len(rows)),
            )
            snapshot_id = int(cur.lastrowid or 0)
            cur.executemany(
                "INSERT INTO snapshot_row (snapshot_id, name, data) VALUES (?, ?, ?)",
                [(snapshot_id, row.name, json.dumps(row.data)) for row in rows],
            )
        self._conn.commit()
        return snapshot_id

    def load_snapshot(self, snapshot_id: int) -> list[Row]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT name, data FROM snapshot_row WHERE snapshot_id = ? ORDER BY id",
                (snapshot_id,),
            )
            return [Row(name=r["name"], data=json.loads(r["data"])) for r in cur.fetchall()]

    def snapshots(self, side: str | None = None) -> list[SnapshotInfo]:
        """Newest first -- the one somebody wants is almost always the last one."""
        sql = "SELECT * FROM snapshot"
        params: tuple[object, ...] = ()
        if side is not None:
            sql += " WHERE side = ?"
            params = (side,)
        sql += " ORDER BY id DESC"
        with closing(self._conn.cursor()) as cur:
            cur.execute(sql, params)
            return [
                SnapshotInfo(
                    id=r["id"],
                    side=r["side"],
                    label=r["label"],
                    source=r["source"],
                    loaded_at=r["loaded_at"],
                    row_count=r["row_count"],
                    key_column=r["key_column"],
                )
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def save_run(
        self,
        master_snapshot_id: int,
        target_snapshot_id: int,
        reconciliation: Reconciliation,
        note: str = "",
        vault_snapshot_id: int | None = None,
    ) -> int:
        """Store a comparison. `vault_snapshot_id` is None for a run made with
        no Data Vault list -- a legitimate state, not missing data."""
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO run "
                "(master_snapshot_id, target_snapshot_id, vault_snapshot_id, "
                "ran_at, note) VALUES (?, ?, ?, ?, ?)",
                (
                    master_snapshot_id,
                    target_snapshot_id,
                    vault_snapshot_id,
                    _now(),
                    note,
                ),
            )
            run_id = int(cur.lastrowid or 0)
            cur.executemany(
                "INSERT INTO run_result (run_id, outcome, name, side, note, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        row.outcome.value,
                        row.name,
                        row.source,
                        row.note,
                        json.dumps(row.data),
                    )
                    for row in reconciliation.rows
                ],
            )
        self._conn.commit()
        return run_id

    def load_run(self, run_id: int) -> Reconciliation:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT outcome, name, side, note, data FROM run_result "
                "WHERE run_id = ? ORDER BY id",
                (run_id,),
            )
            rows = tuple(
                ResultRow(
                    outcome=Outcome(r["outcome"]),
                    name=r["name"],
                    source=r["side"],
                    note=r["note"],
                    data=json.loads(r["data"]),
                )
                for r in cur.fetchall()
            )
            cur.execute(
                "SELECT s1.row_count AS master_count, s2.row_count AS target_count, "
                "s3.row_count AS vault_count, run.vault_snapshot_id "
                "FROM run "
                "JOIN snapshot s1 ON s1.id = run.master_snapshot_id "
                "JOIN snapshot s2 ON s2.id = run.target_snapshot_id "
                "LEFT JOIN snapshot s3 ON s3.id = run.vault_snapshot_id "
                "WHERE run.id = ?",
                (run_id,),
            )
            counts = cur.fetchone()
        return Reconciliation(
            rows=rows,
            master_count=counts["master_count"] if counts else 0,
            target_count=counts["target_count"] if counts else 0,
            vault_count=(counts["vault_count"] or 0) if counts else 0,
            vault_checked=bool(counts and counts["vault_snapshot_id"] is not None),
        )

    def runs(self) -> list[RunInfo]:
        """Newest first, with the labels of both sides joined in."""
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT run.*, s1.label AS master_label, s2.label AS target_label "
                "FROM run "
                "JOIN snapshot s1 ON s1.id = run.master_snapshot_id "
                "JOIN snapshot s2 ON s2.id = run.target_snapshot_id "
                "ORDER BY run.id DESC"
            )
            return [
                RunInfo(
                    id=r["id"],
                    master_snapshot_id=r["master_snapshot_id"],
                    target_snapshot_id=r["target_snapshot_id"],
                    vault_snapshot_id=r["vault_snapshot_id"],
                    ran_at=r["ran_at"],
                    note=r["note"],
                    master_label=r["master_label"],
                    target_label=r["target_label"],
                )
                for r in cur.fetchall()
            ]

    def compare_runs(self, earlier_run_id: int, later_run_id: int) -> dict[str, list[str]]:
        """What changed between two runs, by outcome.

        Returns names that became a finding, names that stopped being one, and
        names whose outcome changed at all. This is the question the snapshot
        history exists to answer -- "is this new, or has it been missing all
        along" -- and answering it by eye across two CSV exports is exactly the
        manual step worth removing.
        """
        earlier = {row.name: row.outcome for row in self.load_run(earlier_run_id).rows}
        later = {row.name: row.outcome for row in self.load_run(later_run_id).rows}

        appeared = sorted(name for name in later if name not in earlier)
        disappeared = sorted(name for name in earlier if name not in later)
        changed = sorted(
            f"{name}: {earlier[name].value} -> {later[name].value}"
            for name in later
            if name in earlier and earlier[name] is not later[name]
        )
        return {"appeared": appeared, "disappeared": disappeared, "changed": changed}
