from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from reconcile import Outcome, Reconciliation, ResultRow, Row, SizeEntry, fingerprint

DEFAULT_DB_PATH = Path(__file__).with_name("reconcile.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id          INTEGER PRIMARY KEY,
    side        TEXT    NOT NULL
                CHECK (side IN ('master', 'scope', 'platform', 'vault')),
    label       TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    key_column  TEXT    NOT NULL DEFAULT '',
    fingerprint TEXT    NOT NULL DEFAULT '',
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
    -- Nullable, like vault and scope: step 3 is optional, because the Data
    -- Bridge extract cannot be fetched from the API yet and a run against
    -- Data Vault alone is a legitimate reconciliation. NOT NULL here would
    -- have made the store refuse to record a run the tool can perform.
    target_snapshot_id INTEGER          REFERENCES snapshot(id),
    vault_snapshot_id  INTEGER          REFERENCES snapshot(id),
    scope_snapshot_id  INTEGER          REFERENCES snapshot(id),
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

-- Reference data (TASK-0108): a point-in-time snapshot of database sizes,
-- laid onto each fresh inventory. Kept apart from snapshot/run on purpose:
-- it is not an input to any one run, and purging old runs must not touch it.
-- Every load is kept; the latest is the one applied.
CREATE TABLE IF NOT EXISTS size_reference (
    id          INTEGER PRIMARY KEY,
    label       TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    as_of       TEXT    NOT NULL,
    loaded_at   TEXT    NOT NULL,
    row_count   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS size_reference_row (
    id           INTEGER PRIMARY KEY,
    reference_id INTEGER NOT NULL REFERENCES size_reference(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    server       TEXT    NOT NULL DEFAULT '',
    size_mb      REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_size_reference_row_ref ON size_reference_row(reference_id);
"""

def _placeholders(count: int) -> str:
    return ",".join("?" * count)

def _combine(*fingerprints: str) -> str:
    digest = hashlib.blake2b(digest_size=6)
    for value in fingerprints:
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()

def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")

SIDES = ("master", "scope", "platform", "vault")

@dataclass(frozen=True)
class SnapshotInfo:
    id: int
    side: str
    label: str
    source: str
    loaded_at: str
    row_count: int
    key_column: str = ""
    fingerprint: str = ""

@dataclass(frozen=True)
class PurgeReport:

    runs_deleted: int
    snapshots_deleted: int
    result_rows_deleted: int
    snapshot_rows_deleted: int
    runs_kept: tuple[int, ...] = ()

    @property
    def nothing_to_do(self) -> bool:
        return self.runs_deleted == 0

    def summary(self) -> str:
        if self.nothing_to_do:
            return "Nothing to purge."
        return (
            f"{self.runs_deleted} run(s), {self.snapshots_deleted} snapshot(s), "
            f"{self.result_rows_deleted + self.snapshot_rows_deleted} stored rows"
        )

@dataclass(frozen=True)
class RunInfo:
    id: int
    master_snapshot_id: int
    target_snapshot_id: int | None
    ran_at: str
    note: str
    vault_snapshot_id: int | None = None
    scope_snapshot_id: int | None = None
    master_label: str = ""
    target_label: str = ""
    master_count: int = 0
    target_count: int = 0
    vault_count: int | None = None
    fingerprint: str = ""

    @property
    def inputs(self) -> str:
        vault = "no vault" if self.vault_count is None else f"{self.vault_count} vault"
        platform = (
            f"{self.target_count} platform"
            if self.target_snapshot_id is not None
            else "no platform"
        )
        return (
            f"{self.master_count} master / {platform} / "
            f"{vault} · {self.fingerprint}"
        )

@dataclass(frozen=True)
class SizeReferenceInfo:

    id: int
    label: str
    source: str
    as_of: str
    loaded_at: str
    row_count: int

class Store:

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with closing(self._conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT sql FROM sqlite_master WHERE name = 'snapshot'")
            row = cur.fetchone()
            needs_rebuild = bool(row) and "'scope'" not in (row["sql"] or "")

            cur.execute("PRAGMA table_info(run)")
            run_columns = {r["name"] for r in cur.fetchall()}

        if "scope_snapshot_id" not in run_columns:
            with closing(self._conn.cursor()) as cur:
                cur.execute(
                    "ALTER TABLE run ADD COLUMN scope_snapshot_id INTEGER "
                    "REFERENCES snapshot(id)"
                )
            self._conn.commit()

        if needs_rebuild:
            self._conn.execute("PRAGMA foreign_keys = OFF")
            self._conn.execute("PRAGMA legacy_alter_table = ON")
            with closing(self._conn.cursor()) as cur:
                cur.execute("PRAGMA table_info(snapshot)")
                existing = {r["name"] for r in cur.fetchall()}
                key_col = "key_column" if "key_column" in existing else "''"
                fp_col = "fingerprint" if "fingerprint" in existing else "''"

                cur.execute("ALTER TABLE snapshot RENAME TO snapshot_old")
                cur.execute(
                    "CREATE TABLE snapshot ("
                    " id INTEGER PRIMARY KEY,"
                    " side TEXT NOT NULL CHECK (side IN"
                    "  ('master', 'scope', 'platform', 'vault')),"
                    " label TEXT NOT NULL,"
                    " source TEXT NOT NULL,"
                    " key_column TEXT NOT NULL DEFAULT '',"
                    " fingerprint TEXT NOT NULL DEFAULT '',"
                    " loaded_at TEXT NOT NULL,"
                    " row_count INTEGER NOT NULL)"
                )
                cur.execute(
                    "INSERT INTO snapshot (id, side, label, source, key_column,"
                    " fingerprint, loaded_at, row_count) "
                    f"SELECT id, side, label, source, {key_col}, {fp_col},"
                    " loaded_at, row_count FROM snapshot_old"
                )
                cur.execute("DROP TABLE snapshot_old")
            self._conn.commit()
            self._conn.execute("PRAGMA legacy_alter_table = OFF")
            self._conn.execute("PRAGMA foreign_keys = ON")

        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT sql FROM sqlite_master WHERE name = 'run'")
            row = cur.fetchone()
            sql = (row["sql"] or "") if row else ""
        if "target_snapshot_id INTEGER NOT NULL" in sql:
            self._conn.execute("PRAGMA foreign_keys = OFF")
            self._conn.execute("PRAGMA legacy_alter_table = ON")
            with closing(self._conn.cursor()) as cur:
                cur.execute("ALTER TABLE run RENAME TO run_old")
                cur.execute(
                    "CREATE TABLE run ("
                    " id INTEGER PRIMARY KEY,"
                    " master_snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),"
                    " target_snapshot_id INTEGER REFERENCES snapshot(id),"
                    " vault_snapshot_id INTEGER REFERENCES snapshot(id),"
                    " scope_snapshot_id INTEGER REFERENCES snapshot(id),"
                    " ran_at TEXT NOT NULL,"
                    " note TEXT NOT NULL DEFAULT '')"
                )
                cur.execute(
                    "INSERT INTO run (id, master_snapshot_id, target_snapshot_id,"
                    " vault_snapshot_id, scope_snapshot_id, ran_at, note) "
                    "SELECT id, master_snapshot_id, target_snapshot_id,"
                    " vault_snapshot_id, scope_snapshot_id, ran_at, note "
                    "FROM run_old"
                )
                cur.execute("DROP TABLE run_old")
            self._conn.commit()
            self._conn.execute("PRAGMA legacy_alter_table = OFF")
            self._conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def save_snapshot(
        self,
        side: str,
        label: str,
        source: str,
        rows: Sequence[Row],
        key_column: str = "",
    ) -> int:
        if side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}, not {side!r}")
        if not key_column and rows:
            key_column = rows[0].data.get("_key_column", "")
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO snapshot "
                "(side, label, source, key_column, fingerprint, loaded_at, row_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    side,
                    label,
                    source,
                    key_column,
                    fingerprint(rows),
                    _now(),
                    len(rows),
                ),
            )
            snapshot_id = int(cur.lastrowid or 0)
            cur.executemany(
                "INSERT INTO snapshot_row (snapshot_id, name, data) VALUES (?, ?, ?)",
                [(snapshot_id, row.name, json.dumps(row.data)) for row in rows],
            )
        self._conn.commit()
        return snapshot_id

    def save_size_reference(
        self, *, label: str, source: str, as_of: str, entries: Sequence[SizeEntry]
    ) -> int:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO size_reference (label, source, as_of, loaded_at, row_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (label, source, as_of, _now(), len(entries)),
            )
            reference_id = int(cur.lastrowid or 0)
            cur.executemany(
                "INSERT INTO size_reference_row (reference_id, name, server, size_mb) "
                "VALUES (?, ?, ?, ?)",
                [(reference_id, e.name, e.server, e.size_mb) for e in entries],
            )
        self._conn.commit()
        return reference_id

    def latest_size_reference(self) -> tuple[SizeReferenceInfo, list[SizeEntry]] | None:
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT * FROM size_reference ORDER BY id DESC LIMIT 1")
            head = cur.fetchone()
            if head is None:
                return None
            cur.execute(
                "SELECT name, server, size_mb FROM size_reference_row "
                "WHERE reference_id = ? ORDER BY id",
                (head["id"],),
            )
            entries = [
                SizeEntry(name=r["name"], server=r["server"], size_mb=float(r["size_mb"]))
                for r in cur.fetchall()
            ]
        info = SizeReferenceInfo(
            id=head["id"],
            label=head["label"],
            source=head["source"],
            as_of=head["as_of"],
            loaded_at=head["loaded_at"],
            row_count=head["row_count"],
        )
        return info, entries

    def load_snapshot(self, snapshot_id: int) -> list[Row]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT name, data FROM snapshot_row WHERE snapshot_id = ? ORDER BY id",
                (snapshot_id,),
            )
            return [Row(name=r["name"], data=json.loads(r["data"])) for r in cur.fetchall()]

    def snapshots(self, side: str | None = None) -> list[SnapshotInfo]:
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
                    fingerprint=r["fingerprint"],
                )
                for r in cur.fetchall()
            ]

    def save_run(
        self,
        master_snapshot_id: int,
        target_snapshot_id: int | None,
        reconciliation: Reconciliation,
        note: str = "",
        vault_snapshot_id: int | None = None,
        scope_snapshot_id: int | None = None,
    ) -> int:
        if target_snapshot_id is None:
            self._migrate()

        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO run "
                "(master_snapshot_id, target_snapshot_id, vault_snapshot_id, "
                "scope_snapshot_id, ran_at, note) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    master_snapshot_id,
                    target_snapshot_id,
                    vault_snapshot_id,
                    scope_snapshot_id,
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
                "SELECT s1.row_count AS master_count, "
                "s2.row_count AS target_count, "
                "s3.row_count AS vault_count, "
                "run.vault_snapshot_id, run.target_snapshot_id "
                "FROM run "
                "JOIN snapshot s1 ON s1.id = run.master_snapshot_id "
                "LEFT JOIN snapshot s2 ON s2.id = run.target_snapshot_id "
                "LEFT JOIN snapshot s3 ON s3.id = run.vault_snapshot_id "
                "WHERE run.id = ?",
                (run_id,),
            )
            counts = cur.fetchone()
        return Reconciliation(
            rows=rows,
            master_count=counts["master_count"] if counts else 0,
            target_count=(counts["target_count"] or 0) if counts else 0,
            vault_count=(counts["vault_count"] or 0) if counts else 0,
            vault_checked=bool(counts and counts["vault_snapshot_id"] is not None),
            platform_checked=bool(
                counts and counts["target_snapshot_id"] is not None
            ),
        )

    def runs(self) -> list[RunInfo]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT run.*, "
                "s1.label AS master_label, "
                "COALESCE(s2.label, '') AS target_label, "
                "s1.row_count AS master_count, "
                "COALESCE(s2.row_count, 0) AS target_count, "
                "s3.row_count AS vault_count, "
                "s1.fingerprint AS mfp, "
                "COALESCE(s2.fingerprint, '') AS tfp, "
                "COALESCE(s3.fingerprint, '') AS vfp "
                "FROM run "
                "JOIN snapshot s1 ON s1.id = run.master_snapshot_id "
                "LEFT JOIN snapshot s2 ON s2.id = run.target_snapshot_id "
                "LEFT JOIN snapshot s3 ON s3.id = run.vault_snapshot_id "
                "ORDER BY run.id DESC"
            )
            return [
                RunInfo(
                    id=r["id"],
                    master_snapshot_id=r["master_snapshot_id"],
                    target_snapshot_id=r["target_snapshot_id"],
                    vault_snapshot_id=r["vault_snapshot_id"],
                    scope_snapshot_id=r["scope_snapshot_id"],
                    ran_at=r["ran_at"],
                    note=r["note"],
                    master_label=r["master_label"],
                    target_label=r["target_label"],
                    master_count=r["master_count"],
                    target_count=r["target_count"],
                    vault_count=r["vault_count"],
                    fingerprint=_combine(r["mfp"], r["tfp"], r["vfp"]),
                )
                for r in cur.fetchall()
            ]

    def compare_runs(self, earlier_run_id: int, later_run_id: int) -> dict[str, list[str]]:
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

    def purge_preview(self, keep: int) -> PurgeReport:
        return self._purge(keep, dry_run=True)

    def purge(self, keep: int) -> PurgeReport:
        return self._purge(keep, dry_run=False)

    def _purge(self, keep: int, *, dry_run: bool) -> PurgeReport:
        if keep < 0:
            raise ValueError(f"keep must be 0 or more, not {keep}")

        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT id FROM run ORDER BY id DESC")
            all_runs = [r["id"] for r in cur.fetchall()]
            doomed = all_runs[keep:]

            if not doomed:
                return PurgeReport(0, 0, 0, 0, tuple(all_runs))

            placeholders = _placeholders(len(doomed))

            cur.execute(
                f"SELECT COUNT(*) n FROM run_result WHERE run_id IN ({placeholders})",
                doomed,
            )
            results_deleted = cur.fetchone()["n"]

            cur.execute(
                "SELECT id FROM snapshot WHERE id NOT IN ("
                "  SELECT master_snapshot_id FROM run WHERE id NOT IN "
                f"({placeholders})"
                "  UNION SELECT target_snapshot_id FROM run WHERE id NOT IN "
                f"({placeholders})"
                "  UNION SELECT vault_snapshot_id FROM run WHERE id NOT IN "
                f"({placeholders})"
                "    AND vault_snapshot_id IS NOT NULL"
                "  UNION SELECT scope_snapshot_id FROM run WHERE id NOT IN "
                f"({placeholders})"
                "    AND scope_snapshot_id IS NOT NULL"
                ")",
                doomed * 4,
            )
            orphan_snapshots = [r["id"] for r in cur.fetchall()]

            snapshot_rows_deleted = 0
            if orphan_snapshots:
                snap_placeholders = _placeholders(len(orphan_snapshots))
                cur.execute(
                    "SELECT COUNT(*) n FROM snapshot_row "
                    f"WHERE snapshot_id IN ({snap_placeholders})",
                    orphan_snapshots,
                )
                snapshot_rows_deleted = cur.fetchone()["n"]

            if not dry_run:
                cur.execute(
                    f"DELETE FROM run WHERE id IN ({placeholders})", doomed
                )
                if orphan_snapshots:
                    snap_placeholders = _placeholders(len(orphan_snapshots))
                    cur.execute(
                        f"DELETE FROM snapshot WHERE id IN ({snap_placeholders})",
                        orphan_snapshots,
                    )

        if not dry_run:
            self._conn.commit()
            self._conn.execute("VACUUM")

        return PurgeReport(
            runs_deleted=len(doomed),
            snapshots_deleted=len(orphan_snapshots),
            result_rows_deleted=results_deleted,
            snapshot_rows_deleted=snapshot_rows_deleted,
            runs_kept=tuple(all_runs[:keep]),
        )
